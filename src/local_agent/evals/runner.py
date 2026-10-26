"""
Runs the golden set on one or more models and writes the table.

Each task runs in its own throwaway home (never ~/.local-agent): a config naming the world's MCP servers, memory
notes if the task has any, and the real run() through channels.conversation.reply, as the terminal would. Built-in
tools are off, so the fake world is all the run can reach. The owner in this world declines every write.

Models come from the keychain like the app's (`local-agent key set openai|anthropic`); Ollama needs no key. Every
call, the judge's included, goes through the meter: the run stops before a call that could pass --max-cost.
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
from agents import Agent, Model, ModelSettings, Runner, set_tracing_disabled

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
DEFAULT_JUDGE = 'openai:gpt-4o'  # the judge must be at least as capable as what it grades (agent-studio's finding)
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
    """'openai:gpt-4o', 'anthropic:claude-sonnet-4-6', 'ollama:qwen2.5:7b' (the name may have colons), 'scripted'."""
    provider, _, name = text.partition(':')
    if provider == 'scripted':
        return ModelSpec('scripted', name or 'scripted')
    if provider not in PROVIDERS or not name:
        raise SetupError(f'{text!r} is not a model: write provider:name, provider one of {", ".join(PROVIDERS)} '
                         '(e.g. openai:gpt-4o, anthropic:claude-sonnet-4-6, ollama:qwen2.5:7b, scripted).')
    return ModelSpec(provider, name)


def parse_price(text: str) -> tuple[str, Price]:
    """'NAME=IN,OUT' in dollars per million tokens, for a model LiteLLM's price list doesn't have."""
    name, _, prices = text.partition('=')
    try:
        given, taken = (float(part) for part in prices.split(','))
    except ValueError:
        raise SetupError(f'--price {text!r}: write NAME=IN,OUT in $ per million tokens, e.g. '
                         'claude-sonnet-4-6=3,15.') from None
    return name, Price(given / 1e6, taken / 1e6)


# --- setup ------------------------------------------------------------------------------------------------

def check_setup(specs: list[ModelSpec], judge: ModelSpec | None, prices: dict[str, Price],
                ollama_url: str = OLLAMA_URL, transport=None) -> None:
    """Every problem at once, before anything is spent: missing keys, unknown prices, Ollama not running."""
    problems = []
    wanted = [*specs, *([judge] if judge else [])]
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


def build(spec: ModelSpec, meter: Meter, prices: dict[str, Price]) -> Model:
    """A metered model from the keychain, like the app's own (providers.build_model)."""
    config = Config(model=ModelConfig(provider=spec.provider, default=spec.name, strong=spec.name))
    return MeteredModel(build_model(config, spec.name), meter, price_for(spec.provider, spec.name, prices),
                        spec.label)


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

        async with McpConnections(rt.config.mcp_servers) as mcp:
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
    judge: str
    max_cost: float
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec='seconds'))
    results: list[dict] = field(default_factory=list)
    spent: float = 0.0
    judge_cost: float = 0.0
    stopped: str = ''

    def to_json(self) -> dict:
        return {'models': self.models, 'judge': self.judge, 'max_cost': self.max_cost, 'spent': round(self.spent, 6),
                'judge_cost': round(self.judge_cost, 6), 'stopped': self.stopped, 'started_at': self.started_at,
                'results': self.results}


def _tokens(n: int) -> str:
    return f'{n / 1000:.1f}k' if n >= 1000 else str(n)


def _cell(result: dict | None) -> str:
    if result is None:
        return 'not run'
    if result['status'] == 'stopped':
        return 'stopped (cap)'
    verdict = 'pass' if result['passed'] else 'FAIL'
    return f'{verdict} · {_tokens(result["tokens"])} tok · ${result["cost"]:.3f}'


def markdown(report: Report) -> str:
    by = {(r['model'], r['task']): r for r in report.results}
    lines = ['| task | ' + ' | '.join(report.models) + ' |', '|---|' + '---|' * len(report.models)]
    for task in TASKS:
        lines.append(f'| {task["id"]} | ' + ' | '.join(_cell(by.get((m, task['id']))) for m in report.models) + ' |')
    totals = []
    for model in report.models:
        mine = [r for r in report.results if r['model'] == model and r['status'] == 'ran']
        totals.append(f'{sum(r["passed"] for r in mine)}/{len(TASKS)} · '
                      f'{_tokens(sum(r["tokens"] for r in mine))} tok · ${sum(r["cost"] for r in mine):.3f}')
    lines.append('| **total** | ' + ' | '.join(totals) + ' |')
    lines += ['', f'Judge: {report.judge} (${report.judge_cost:.3f}). Spent ${report.spent:.3f} of the '
                  f'${report.max_cost:.2f} cap.']
    if report.stopped:
        lines.append(f'**{report.stopped}** The cells marked "stopped (cap)" or "not run" have no result.')
    failures = [r for r in report.results if r['status'] == 'ran' and not r['passed']]
    if failures:
        lines += ['', 'Failures:']
        for r in failures:
            why = [f'checks: {", ".join(r["failed_checks"])}'] if r['failed_checks'] else []
            if r['rubric_pass'] is False:
                why.append(f'rubric: {r["rubric_reason"]}')
            if r['outcome'] != 'answered':
                why.append(f'run {r["outcome"]}')
            lines.append(f'- {r["model"]} / {r["task"]}: {"; ".join(why)}')
    return '\n'.join(lines) + '\n'


async def run_evals(specs: list[ModelSpec], judge_spec: ModelSpec | None, out: Path, *, max_cost: float = 3.0,
                    prices: dict[str, Price] | None = None, task_ids: list[str] | None = None,
                    model_factory=None, judge_factory=None, say=print) -> Report:
    """
    Every task on every model, task by task (so a stop at the cap leaves every model with the same tasks done).
    model_factory(spec, task_id) and judge_factory() replace the keychain models (the scripted run, tests).
    """
    set_tracing_disabled(True)  # the SDK would otherwise send traces of these runs to OpenAI
    prices = prices or {}
    meter = Meter(max_cost)
    report = Report(models=[spec.label for spec in specs], judge=judge_spec.label if judge_spec else 'none',
                    max_cost=max_cost)
    tasks = [task for task in TASKS if not task_ids or task['id'] in task_ids]

    def model_for(spec: ModelSpec, task_id: str) -> Model:
        if model_factory:
            return MeteredModel(model_factory(spec, task_id), meter, price_for(spec.provider, spec.name, prices),
                                spec.label)
        if spec.provider == 'scripted':
            return MeteredModel(scripted.scripted_model(task_id), meter, price_for('scripted', spec.name),
                                spec.label)
        return built.setdefault(spec, build(spec, meter, prices))

    built: dict[ModelSpec, Model] = {}
    judge_model = None
    if judge_spec:
        inner = judge_factory() if judge_factory else (scripted.scripted_judge() if judge_spec.provider == 'scripted'
                                                       else None)
        judge_model = (MeteredModel(inner, meter, price_for(judge_spec.provider, judge_spec.name, prices),
                                    f'{judge_spec.label} (judge)') if inner else build(judge_spec, meter, prices))

    for task in tasks:
        for spec in specs:
            entry = {'model': spec.label, 'task': task['id'], 'status': 'stopped', 'passed': False, 'tokens': 0,
                     'cost': 0.0, 'judge_cost': 0.0, 'failed_checks': [], 'rubric_pass': None, 'rubric_reason': '',
                     'outcome': '', 'tools': [], 'answer': '', 'run_id': '', 'seconds': 0.0}
            report.results.append(entry)
            if meter.stopped:
                continue
            before, started = meter.spent, time.monotonic()
            folder = out / 'runs' / re.sub(r'[^A-Za-z0-9._-]+', '-', spec.label) / task['id']
            try:
                run, result = await run_task(task, model_for(spec, task['id']), folder)
            except BudgetReached:
                continue
            entry['cost'] = meter.spent - before
            if meter.stopped:  # the cap was reached inside the run: its result is incomplete
                continue
            entry.update(status='ran', outcome=result.outcome, tokens=views.tokens_used(run.events),
                         tools=run.called(), answer=run.answer, failed_checks=failed_checks(task, run),
                         run_id=result.run_id, home=str(folder / 'home'), seconds=round(time.monotonic() - started, 1))
            if judge_model is not None:
                judged_from = meter.spent
                try:
                    entry['rubric_pass'], entry['rubric_reason'] = await judge(judge_model, task['rubric'],
                                                                               task['message'], run.answer)
                except BudgetReached:
                    entry['rubric_reason'] = 'not judged: the cap was reached'
                except Exception as e:
                    entry['rubric_pass'], entry['rubric_reason'] = False, f'the judge failed: {type(e).__name__}'
                entry['judge_cost'] = meter.spent - judged_from
                report.judge_cost += entry['judge_cost']
            entry['passed'] = (result.outcome == 'answered' and not entry['failed_checks']
                               and (judge_model is None or entry['rubric_pass'] is True))
            say(f'[{spec.label} / {task["id"]}] {_cell(entry)}'
                + (f' | failed: {entry["failed_checks"]}' if entry['failed_checks'] else '')
                + (f' | rubric: {entry["rubric_reason"]}' if entry['rubric_pass'] is False else ''))

    report.spent, report.stopped = meter.spent, meter.stopped
    out.mkdir(parents=True, exist_ok=True)
    (out / 'report.json').write_text(json.dumps(report.to_json(), indent=1))
    (out / 'report.md').write_text(markdown(report))
    return report
