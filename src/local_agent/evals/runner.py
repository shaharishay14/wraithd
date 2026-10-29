"""
Runs the golden set on one or more models and writes the table.

Each task runs in its own throwaway home (never ~/.local-agent): a config naming the world's MCP servers, memory
notes if the task has any, and the real run() through channels.conversation.reply, as the terminal would. Built-in
tools are off, so the fake world is all the run can reach. The owner in this world declines every write.

Models come from the keychain like the app's (`local-agent key set openai|anthropic`); Ollama, or any
OpenAI-compatible server at --ollama-url, needs no key. Every call, the judges' included, goes through the meter: the
run stops before a call that could pass --max-cost. --runs repeats each task; every judge grades every answer.
"""

import asyncio
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import keyring
from agents import Agent, Model, ModelSettings, Runner

from local_agent.channels.conversation import reply
from local_agent.config import APP, Config, Home, ModelConfig
from local_agent.core import views
from local_agent.evals import scripted
from local_agent.evals.meter import BudgetReached, Meter, MeteredModel, Price, price_for
from local_agent.evals.tasks import TASKS, Run, failed_checks
from local_agent.mcp.client import McpConnections, tool_name
from local_agent.memory.notes import Note
from local_agent.providers import OLLAMA_URL, build_model
from local_agent.run import RunResult, Runtime
from local_agent.store import base as store_base

PROVIDERS = ('openai', 'anthropic', 'ollama', 'scripted')
KEYED = ('openai', 'anthropic')
# Two judges from two vendors, each at least as capable as what it grades (agent-studio's finding)
DEFAULT_JUDGES = ('openai:gpt-4o', 'anthropic:claude-sonnet-5-5')
JUDGE_TIMEOUT_SECONDS = 60


class SetupError(Exception):
    """The eval can't start as asked. The message says what to do."""


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    name: str

    @property
    def label(self) -> str:
        return 'scripted' if (self.provider, self.name) == ('scripted', 'scripted') else f'{self.provider}:{self.name}'


def parse_spec(text: str) -> ModelSpec:
    """'openai:gpt-4o', 'anthropic:claude-sonnet-5-5', 'ollama:qwen2.5:7b' (the name may have colons), 'scripted'."""
    provider, _, name = text.partition(':')
    if provider == 'scripted':
        return ModelSpec('scripted', name or 'scripted')
    if provider not in PROVIDERS or not name:
        raise SetupError(f'{text!r} is not a model: write provider:name, provider one of {", ".join(PROVIDERS)} '
                         '(e.g. openai:gpt-4o, anthropic:claude-sonnet-5-5, ollama:qwen2.5:7b, scripted).')
    return ModelSpec(provider, name)


def parse_price(text: str) -> tuple[str, Price]:
    """'NAME=IN,OUT' in dollars per million tokens, for a model LiteLLM's price list doesn't have."""
    name, _, prices = text.partition('=')
    try:
        given, taken = (float(part) for part in prices.split(','))
    except ValueError:
        raise SetupError(f'--price {text!r}: write NAME=IN,OUT in $ per million tokens, e.g. '
                         'claude-sonnet-5-5=2,10.') from None
    return name, Price(given / 1e6, taken / 1e6)


# --- setup ------------------------------------------------------------------------------------------------

def check_setup(specs: list[ModelSpec], judges: list[ModelSpec], prices: dict[str, Price],
                ollama_url: str = OLLAMA_URL, transport=None) -> None:
    """Every problem at once, before anything is spent: missing keys, unknown prices, Ollama not running."""
    problems = []
    wanted = [*specs, *judges]
    for provider in dict.fromkeys(spec.provider for spec in wanted if spec.provider in KEYED):
        if not keyring.get_password(APP, provider):
            problems.append(f'No {provider} key in the keychain. Set one with: local-agent key set {provider}')
    for spec in dict.fromkeys(wanted):
        if price_for(spec.provider, spec.name, prices) is None:
            problems.append(f'No price for {spec.name} in LiteLLM\'s list, so the cap can\'t be kept. Pass '
                            f'--price {spec.name}=IN,OUT ($ per million input and output tokens).')
    if any(spec.provider == 'ollama' for spec in wanted):
        try:
            with httpx.Client(transport=transport, timeout=3) as client:
                client.get(f'{ollama_url.rstrip("/")}/models').raise_for_status()
        except httpx.HTTPError:
            problems.append(f'Ollama isn\'t answering at {ollama_url}. Start it (ollama serve) and pull the model '
                            '(ollama pull NAME).')
    if problems:
        raise SetupError('\n'.join(problems))


def build(spec: ModelSpec, meter: Meter, prices: dict[str, Price], ollama_url: str = OLLAMA_URL,
          label: str = '') -> Model:
    """A metered model from the keychain, like the app's own (providers.build_model). Ollama at ollama_url."""
    config = Config(model=ModelConfig(provider=spec.provider, default=spec.name, strong=spec.name,
                                      base_url=ollama_url if spec.provider == 'ollama' else ''))
    return MeteredModel(build_model(config, spec.name), meter, price_for(spec.provider, spec.name, prices),
                        label or spec.label)


# --- one task ---------------------------------------------------------------------------------------------

def world_config(task: dict, logs: Path) -> str:
    """config.toml for the task: no web search, and one [mcp.servers.*] block per app it uses."""
    lines = ['[web]', 'search = ""', '']
    provides = {'gmail': ['email'], 'googlecalendar': ['calendar'], 'web': ['web_search']}
    for app in task.get('apps', ()):
        args = ['-m', 'local_agent.evals.world', '--app', app, '--log', str(logs / f'{app}.jsonl')]
        if app == 'googlecalendar' and task.get('filler'):
            args += ['--filler', str(task['filler'])]
        lines += [f'[mcp.servers.{app}]', 'transport = "stdio"', f'command = {json.dumps(sys.executable)}',
                  f'args = {json.dumps(args)}', f'provides = {json.dumps(provides.get(app, []))}', '']
    return '\n'.join(lines)


def app_calls(logs: Path) -> list[tuple[str, dict]]:
    calls = []
    for path in sorted(logs.glob('*.jsonl')):
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            calls.append((tool_name(path.stem, entry['tool']), entry['arguments']))
    return calls


async def run_task(task: dict, model: Model, folder: Path) -> tuple[Run, RunResult]:
    """One task in a fresh home under folder. Returns what the checks look at, and the run's result."""
    shutil.rmtree(folder, ignore_errors=True)  # an earlier eval run's home for this task
    logs = folder / 'world'
    logs.mkdir(parents=True)
    home = Home(folder / 'home')
    home.ensure()
    home.config_path.write_text(world_config(task, logs))
    rt = Runtime.open(home, models={'default': model})
    try:
        for path, body in task.get('notes', {}).items():
            rt.memory.save(Note(path=path, title=Path(path).stem.title(), body=body))

        async def decline(event_type, data):  # the owner in this world says no to every write
            if event_type == 'approval_required':
                rt.store.decide_approval(data['approval_id'], store_base.DENIED)

        async with McpConnections(rt.config.mcp_servers, store=rt.store) as mcp:
            if mcp.errors:
                raise RuntimeError(f'world servers did not start: {mcp.errors}')
            conversation = rt.store.start_conversation('eval')
            options = {'callback': decline, 'mcp': mcp, 'builtins': False}
            if task.get('before'):
                await reply(rt, conversation.id, task['before'], **options)
            result = await reply(rt, conversation.id, task['message'], **options)
        return Run(result.answer, rt.store.run_events(result.run_id), app_calls(logs)), result
    finally:
        rt.close()


async def judge(model: Model, rubric: str, message: str, answer: str) -> tuple[bool, str]:
    agent = Agent(name='judge', instructions='You grade answers. Reply with JSON only.', model=model,
                  model_settings=ModelSettings(temperature=0))
    prompt = ('Grade an AI assistant\'s answer against a rubric. Reply with JSON {"pass": true|false, "reason": "..."}.'
              f'\n\nUser asked: {message}\n\nRubric: {rubric}\n\nAnswer:\n{answer}')
    result = await asyncio.wait_for(Runner.run(agent, prompt, max_turns=1), timeout=JUDGE_TIMEOUT_SECONDS)
    text = str(result.final_output or '')
    found = re.search(r'\{.*\}', text, re.DOTALL)
    try:
        verdict = json.loads(found.group(0)) if found else {}
    except json.JSONDecodeError:
        verdict = {}
    if not isinstance(verdict, dict) or 'pass' not in verdict:
        return False, f'the judge did not answer in JSON: {text[:200]}'
    return bool(verdict.get('pass')), str(verdict.get('reason', ''))


# --- the table --------------------------------------------------------------------------------------------

@dataclass
class Report:
    models: list[str]
    judges: list[str]
    max_cost: float
    runs: int = 1
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec='seconds'))
    results: list[dict] = field(default_factory=list)
    spent: float = 0.0
    judge_cost: float = 0.0
    stopped: str = ''

    def to_json(self) -> dict:
        return {'models': self.models, 'judges': self.judges, 'runs': self.runs, 'max_cost': self.max_cost,
                'spent': round(self.spent, 6), 'judge_cost': round(self.judge_cost, 6), 'stopped': self.stopped,
                'started_at': self.started_at, 'agreement': agreement(self.results), 'results': self.results}


def _tokens(n: float) -> str:
    return f'{n / 1000:.1f}k' if n >= 1000 else str(round(n))


def _cell(results: list[dict], runs: int) -> str:
    """One model on one task: passes out of the runs asked for, and the mean tokens and cost of the runs that ran."""
    ran = [r for r in results if r['status'] == 'ran']
    if not ran:
        return 'stopped (cap)' if results else 'not run'
    passed = sum(r['passed'] for r in ran)
    cell = (f'pass {passed}/{runs} · {_tokens(sum(r["tokens"] for r in ran) / len(ran))} tok · '
            f'${sum(r["cost"] for r in ran) / len(ran):.3f}')
    return cell + (f' ({runs - len(ran)} stopped)' if len(ran) < runs else '')


def _line(result: dict) -> str:
    if result['status'] != 'ran':
        return 'stopped (cap)'
    return (f'{"pass" if result["passed"] else "FAIL"} · {_tokens(result["tokens"])} tok · '
            f'${result["cost"]:.3f}')


def agreement(results: list[dict]) -> dict:
    """How often the judges agreed, over the answers every judge graded (needs two judges or more)."""
    graded = [r['judges'] for r in results
              if len(r.get('judges', {})) > 1 and all(v['pass'] is not None for v in r['judges'].values())]
    agreed = sum(len({v['pass'] for v in verdicts.values()}) == 1 for verdicts in graded)
    return {'graded': len(graded), 'agreed': agreed}


def _judged(result: dict) -> list[str]:
    return [f'{name} {"pass" if v["pass"] else "fail" if v["pass"] is False else "not judged"}'
            for name, v in result['judges'].items()]


def markdown(report: Report) -> str:
    by: dict[tuple, list] = {}
    for r in report.results:
        by.setdefault((r['model'], r['task']), []).append(r)
    lines = ['| task | ' + ' | '.join(report.models) + ' |', '|---|' + '---|' * len(report.models)]
    task_ids = list(dict.fromkeys(r['task'] for r in report.results)) or [task['id'] for task in TASKS]
    for task_id in task_ids:
        lines.append(f'| {task_id} | '
                     + ' | '.join(_cell(by.get((m, task_id), []), report.runs) for m in report.models) + ' |')
    totals = []
    for model in report.models:
        mine = [r for r in report.results if r['model'] == model and r['status'] == 'ran']
        totals.append(f'{sum(r["passed"] for r in mine)}/{len(task_ids) * report.runs} · '
                      f'{_tokens(sum(r["tokens"] for r in mine))} tok · ${sum(r["cost"] for r in mine):.3f}')
    lines.append('| **total** | ' + ' | '.join(totals) + ' |')
    runs = 'one run' if report.runs == 1 else f'{report.runs} runs'
    lines += ['', f'Each cell: passes out of {runs} per task, then the mean tokens and cost of a run. The total row '
                  'adds up every run.']
    if report.judges:
        agreed = agreement(report.results)
        share = f' ({100 * agreed["agreed"] / agreed["graded"]:.0f}%)' if agreed['graded'] else ''
        lines.append(f'Judges: {", ".join(report.judges)} (${report.judge_cost:.3f}). A rubric passes only when '
                     'every judge passes it.'
                     + (f' The judges agreed on {agreed["agreed"]} of {agreed["graded"]} answers{share}.'
                        if len(report.judges) > 1 else ''))
    else:
        lines.append('Judges: none (checks only).')
    lines.append(f'Spent ${report.spent:.3f} of the ${report.max_cost:.2f} cap.')
    if report.stopped:
        lines.append(f'**{report.stopped}** The cells marked "stopped" or "not run" have no result.')
    failures = [r for r in report.results if r['status'] == 'ran' and not r['passed']]
    run_tag = (lambda r: f' (run {r["run"]})') if report.runs > 1 else (lambda r: '')
    if failures:
        lines += ['', 'Failures:']
        for r in failures:
            why = [f'checks: {", ".join(r["failed_checks"])}'] if r['failed_checks'] else []
            if r['rubric_pass'] is False:
                why.append(f'rubric: {r["rubric_reason"]}')
            if r['outcome'] != 'answered':
                why.append(f'run {r["outcome"]}')
            lines.append(f'- {r["model"]} / {r["task"]}{run_tag(r)}: {"; ".join(why)}')
    split = [r for r in report.results if len({v['pass'] for v in r.get('judges', {}).values()}) > 1]
    if split:
        lines += ['', 'The judges disagreed:']
        lines += [f'- {r["model"]} / {r["task"]}{run_tag(r)}: {", ".join(_judged(r))}' for r in split]
    return '\n'.join(lines) + '\n'


async def run_evals(specs: list[ModelSpec], judge_specs: list[ModelSpec], out: Path, *, max_cost: float = 3.0,
                    runs: int = 1, prices: dict[str, Price] | None = None, task_ids: list[str] | None = None,
                    ollama_url: str = OLLAMA_URL, model_factory=None, judge_factory=None, say=print) -> Report:
    """
    Every task on every model, runs times, task by task and round by round (so a stop at the cap leaves every model
    with the same runs done). Every judge grades every answer; the rubric passes only if they all pass it.
    model_factory(spec, task_id) and judge_factory(spec) replace the keychain models (the scripted run, tests).
    """
    prices = prices or {}
    meter = Meter(max_cost)
    report = Report(models=[spec.label for spec in specs], judges=[spec.label for spec in judge_specs],
                    max_cost=max_cost, runs=runs)
    tasks = [task for task in TASKS if not task_ids or task['id'] in task_ids]
    built: dict[ModelSpec, Model] = {}

    def model_for(spec: ModelSpec, task_id: str) -> Model:
        if model_factory:
            return MeteredModel(model_factory(spec, task_id), meter, price_for(spec.provider, spec.name, prices),
                                spec.label)
        if spec.provider == 'scripted':
            return MeteredModel(scripted.scripted_model(task_id), meter, price_for('scripted', spec.name),
                                spec.label)
        return built.setdefault(spec, build(spec, meter, prices, ollama_url))

    def judge_for(spec: ModelSpec) -> Model:
        inner = judge_factory(spec) if judge_factory else (scripted.scripted_judge(spec.name)
                                                           if spec.provider == 'scripted' else None)
        if inner is None:
            return build(spec, meter, prices, ollama_url, label=f'{spec.label} (judge)')
        return MeteredModel(inner, meter, price_for(spec.provider, spec.name, prices), f'{spec.label} (judge)')

    judges = {spec.label: judge_for(spec) for spec in judge_specs}

    for task in tasks:
        for round_ in range(1, runs + 1):
            for spec in specs:
                entry = {'model': spec.label, 'task': task['id'], 'run': round_, 'status': 'stopped', 'passed': False,
                         'tokens': 0, 'cost': 0.0, 'judge_cost': 0.0, 'failed_checks': [], 'judges': {},
                         'rubric_pass': None, 'rubric_reason': '', 'outcome': '', 'tools': [], 'answer': '',
                         'run_id': '', 'seconds': 0.0}
                report.results.append(entry)
                if meter.stopped:
                    continue
                before, started = meter.spent, time.monotonic()
                folder = out / 'runs' / re.sub(r'[^A-Za-z0-9._-]+', '-', spec.label) / task['id']
                if runs > 1:
                    folder = folder / f'run-{round_}'
                try:
                    run, result = await run_task(task, model_for(spec, task['id']), folder)
                except BudgetReached:
                    continue
                entry['cost'] = meter.spent - before
                if meter.stopped:  # the cap was reached inside the run: its result is incomplete
                    continue
                entry.update(status='ran', outcome=result.outcome, tokens=views.tokens_used(run.events),
                             tools=run.called(), answer=run.answer, failed_checks=failed_checks(task, run),
                             run_id=result.run_id, home=str(folder / 'home'),
                             seconds=round(time.monotonic() - started, 1))
                judged_from = meter.spent
                for label, judge_model in judges.items():
                    verdict = {'pass': None, 'reason': ''}
                    try:
                        verdict['pass'], verdict['reason'] = await judge(judge_model, task['rubric'],
                                                                         task['message'], run.answer)
                    except BudgetReached:
                        verdict['reason'] = 'not judged: the cap was reached'
                    except Exception as e:
                        verdict['pass'], verdict['reason'] = False, f'the judge failed: {type(e).__name__}'
                    entry['judges'][label] = verdict
                entry['judge_cost'] = meter.spent - judged_from
                report.judge_cost += entry['judge_cost']
                if judges:
                    verdicts = entry['judges'].values()
                    entry['rubric_pass'] = (True if all(v['pass'] is True for v in verdicts)
                                            else False if any(v['pass'] is False for v in verdicts) else None)
                    entry['rubric_reason'] = '; '.join(
                        (f'{name}: ' if len(judges) > 1 else '') + v['reason']
                        for name, v in entry['judges'].items() if v['pass'] is not True)
                entry['passed'] = (result.outcome == 'answered' and not entry['failed_checks']
                                   and (not judges or entry['rubric_pass'] is True))
                tag = f' run {round_}' if runs > 1 else ''
                say(f'[{spec.label} / {task["id"]}{tag}] {_line(entry)}'
                    + (f' | failed: {entry["failed_checks"]}' if entry['failed_checks'] else '')
                    + (f' | rubric: {entry["rubric_reason"]}' if entry['rubric_pass'] is False else ''))

    report.spent, report.stopped = meter.spent, meter.stopped
    out.mkdir(parents=True, exist_ok=True)
    (out / 'report.json').write_text(json.dumps(report.to_json(), indent=1))
    (out / 'report.md').write_text(markdown(report))
    return report
