"""
One run: build the context, run the loop, log every step, return the answer.

This is agent-studio's 2,500-line agent_service.run() reduced to what the harness needs: the owner's standing
orders, memory (L0 + L1) and the skills list in the prompt; memory and skill tools behind the permission gate;
reminders; the loop with its stop rules; and an event log that is saved even when the run fails.
"""

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime

from agents import Agent, Model, ModelSettings, Runner, set_tracing_disabled

from local_agent.config import Config, Home, load_config
from local_agent.core import compaction, disclosure, reminders, tool_results, views
from local_agent.core import events as ev
from local_agent.core.gate import PermissionGate, guard_function_tools
from local_agent.core.loop import RunAlreadyStartedError, run_with_step_limit
from local_agent.core.stop import DAILY_BUDGET_MESSAGE, StopPolicy, tokens_used_today
from local_agent.mcp.client import Connected, McpConnections
from local_agent.memory import tools as memory_tools
from local_agent.memory import vault
from local_agent.memory.notes import Vault
from local_agent.providers import build_models, model_for
from local_agent.skills import registry
from local_agent.skills.tools import skill_tools
from local_agent.store import SQLiteStore, Store
from local_agent.tools import builtin_tools
from local_agent.tools.notify import StoreNotifier

logger = logging.getLogger(__name__)

RUN_TIMEOUT_SECONDS = 180
RETRY_DELAYS = (1, 2)  # seconds before the 2nd and 3rd attempt; only for failures before anything happened
INSTRUCTIONS_CHARS = 10_000  # cap on AGENTS.md in the prompt
MCP_DESCRIPTIONS = 'MCP tool descriptions'  # the taint source when MCP tools are given to the model directly

BASE_INSTRUCTIONS = (
    'You are Local Agent, a personal assistant that runs on the user\'s own computer. Be direct and useful. '
    'Use your tools when they help; never claim you did something a tool didn\'t do.\n'
    'Now: {now}.\n'
)


@dataclass
class Runtime:
    """Everything a run needs, opened once per process: the state folder, config, store, models."""

    home: Home
    config: Config
    store: Store
    models: dict[str, Model]  # 'default', and 'strong' if one is configured
    # The web tools' HTTP transports: None is the real network; tests pass httpx.MockTransport
    web_transport: object = None
    search_transport: object = None
    voice_transport: object = None  # the transcription provider's (voice.py)
    notifier: object = None  # where notify_owner delivers; the daemon sets Telegram, else the store's inbox
    memory: Vault = field(init=False)
    skills: registry.SkillFolder = field(init=False)

    def __post_init__(self):
        # The Agents SDK uploads run traces to OpenAI by default (whenever OPENAI_API_KEY is in the environment).
        # A local agent's runs stay local: every entry point (chat, daemon, schedules, evals) builds a Runtime.
        set_tracing_disabled(True)
        self.notifier = self.notifier or StoreNotifier(self.store)
        self.memory = Vault(self.home.memory_dir)
        self.skills = registry.SkillFolder(self.home.skills_dir)

    @classmethod
    def open(cls, home: Home | None = None, models: dict[str, Model] | None = None) -> 'Runtime':
        home = home or Home.locate()
        home.ensure()
        config = load_config(home)
        return cls(home=home, config=config, store=SQLiteStore(home.db_path), models=models or build_models(config))

    def policy(self, budget: dict | None = None) -> StopPolicy:
        """The stop rules for a run. budget overrides (a subagent's share) only ever tighten the configured ones."""
        limits = {'run_tokens': self.config.run_tokens, 'run_tool_calls': self.config.run_tool_calls,
                  'daily_tokens': self.config.daily_tokens}
        for name, value in (budget or {}).items():
            if name not in limits:
                raise ValueError(f'Unknown budget {name!r}; one of {", ".join(limits)}')
            if value is not None:
                limits[name] = value if limits[name] is None else min(value, limits[name])
        used_today = tokens_used_today(self.store) if limits['daily_tokens'] else 0
        return StopPolicy(token_budget=limits['run_tokens'], max_tool_calls=limits['run_tool_calls'],
                          daily_token_budget=limits['daily_tokens'], tokens_before_run=used_today)

    def model_for(self, purpose: str) -> Model:
        """The model for one purpose (providers.PURPOSES)."""
        return model_for(self.models, purpose)

    async def complete(self, prompt: str, purpose: str = 'chat') -> str:
        """One model call without tools, for the harness's own work (summaries, distill)."""
        helper = Agent(name='helper', instructions='Follow the request exactly.', model=self.model_for(purpose))
        result = await asyncio.wait_for(Runner.run(helper, prompt, max_turns=1), timeout=60)
        return str(result.final_output or '')

    def completer(self, purpose: str):
        """complete() for one purpose, as the complete(prompt) callable compaction and distill take."""
        async def complete(prompt: str) -> str:
            return await self.complete(prompt, purpose)
        return complete

    def close(self) -> None:
        self.store.close()


@dataclass(frozen=True)
class RunResult:
    answer: str
    run_id: str
    outcome: str  # answered | refused | failed


def honest_failure_message(completed_writes: list[str], timed_out: bool = False) -> str:
    """
    What the owner is told when a run fails: built from what actually ran, never from a second model guessing.
    Says which actions already happened, so the owner doesn't repeat them (e.g. a second email).
    """
    problem = 'This took too long' if timed_out else 'Something went wrong'
    if completed_writes:
        done = '\n'.join(f'- {action}' for action in completed_writes)
        return (f'{problem} before I could finish. These actions were already done:\n{done}\n'
                'Check them before asking me to repeat anything.')
    return f"{problem} and I couldn't finish. Nothing was changed. Please try again."


def standing_orders(home: Home) -> str:
    """AGENTS.md, the owner's own words: in every run, and what the gate counts as written by the owner."""
    try:
        text = home.agents_md.read_text().strip()
    except FileNotFoundError:
        return ''
    return text[:INSTRUCTIONS_CHARS]


def _strong_upgrade(rt: Runtime, skills: list[registry.SkillSpec]):
    """The loop's upgrade check: once a `model: strong` skill has loaded, switch to the strong model."""
    strong = {skill.name for skill in skills if skill.model == 'strong'}
    if rt.model_for('strong') is rt.model_for('chat') or not strong:
        return None

    def upgrade(events):
        loaded = next((name for name in views.skills_loaded(events) if name in strong), None)
        return (rt.model_for('strong'), loaded) if loaded else None

    return upgrade


async def _run_loop(agent, chat, callback, log, policy, upgrade):
    """The loop with a time limit, retried only when it failed before anything was streamed or any tool ran."""
    for attempt, delay in enumerate((*RETRY_DELAYS, None)):
        try:
            return await asyncio.wait_for(run_with_step_limit(agent, chat, callback, log=log, policy=policy,
                                                              upgrade=upgrade), timeout=RUN_TIMEOUT_SECONDS)
        except (RunAlreadyStartedError, TimeoutError):
            raise
        except Exception as e:
            if delay is None:
                raise
            logger.warning(f'Run attempt {attempt + 1} failed before starting ({type(e).__name__}); retrying')
            await asyncio.sleep(delay)


async def run(rt: Runtime, message: str, *, history=(), callback=None, source: str = 'chat', owner: bool = True,
              tools=(), app_tools=(), app_guides: dict[str, str] | None = None, builtins: bool = True,
              mcp: Connected | None = None, untrusted_source: str = '', images=(), parent_run_id: str | None = None,
              inherited_taint=(), budget: dict | None = None, purpose: str = 'chat',
              allowed_tools=None) -> RunResult:
    """
    Runs the agent on one message.

    history: earlier messages of the conversation (already compacted), as {"role", "content"} items
    callback: async (event_type, data) for streaming and approvals; None when nobody is watching
    owner: the owner is the one talking (every channel today: Telegram answers only its paired owner). With
    owner=False the run gets no memory, files, apps or owner skills, and can't change skills.
    tools: extra tools (a caller's tool replaces a built-in one with the same name);
    app_tools: connected-app tools (Phase 4), discovered past a dozen;
    builtins: the built-in tools (tools/), which depend on owner and on whether anyone can approve
    mcp: the owner's MCP servers, already connected (a terminal session connects once); without it, run() connects
    them for this run. Only for the owner: they're the owner's accounts.
    untrusted_source: the message itself is third-party content (say, an email that starts the run): tainted from
    its first step, and addresses in it don't count as the owner's.
    images: pictures the owner sent with the message (a Telegram photo), as data: URLs. They go to the model with
    the message as image input; the event log and the conversation keep only the text.

    For a run started by another run (a subagent), all off by default:
    parent_run_id: the run that started this one; recorded on run_start
    inherited_taint: (source, text) pairs of third-party content the parent had read: tainted from the first step
    budget: {'run_tokens', 'run_tool_calls', 'daily_tokens'} limits; they only ever tighten the configured ones
    purpose: which model runs it (providers.PURPOSES)
    allowed_tools: names of the only tools the model is offered (app actions included); None offers every one
    """
    async with AsyncExitStack() as stack:
        if owner and mcp is None and rt.config.mcp_servers:
            mcp = await stack.enter_async_context(McpConnections(rt.config.mcp_servers))
        return await _run(rt, message, history=history, callback=callback, source=source, owner=owner, tools=tools,
                          app_tools=app_tools, app_guides=app_guides, builtins=builtins,
                          mcp=mcp if owner else None, untrusted_source=untrusted_source, images=images,
                          parent_run_id=parent_run_id, inherited_taint=tuple(inherited_taint), budget=budget,
                          purpose=purpose, allowed_tools=None if allowed_tools is None else set(allowed_tools))


def user_content(message: str, images=()):
    """The owner's message as model input: plain text, or text and images (the SDK turns input_image into each
    provider's own image part, through LiteLLM too)."""
    if not images:
        return message
    return [{'type': 'input_text', 'text': message},
            *({'type': 'input_image', 'image_url': image, 'detail': 'auto'} for image in images)]


async def _run(rt, message, *, history, callback, source, owner, tools, app_tools, app_guides, builtins, mcp,
               untrusted_source, images, parent_run_id, inherited_taint, budget, purpose, allowed_tools):
    started = time.monotonic()
    history = list(history)
    log = ev.EventLog(rt.store)
    log.append(ev.RUN_START, source=source, message=message, history_messages=len(history), owner=owner,
               **({'images': len(images)} if images else {}),
               untrusted_inputs=[untrusted_source] if untrusted_source else [], parent_run_id=parent_run_id,
               **({'inherited_taint': [source for source, _ in inherited_taint]} if inherited_taint else {}),
               **({'purpose': purpose} if purpose != 'chat' else {}))
    if callback:
        await callback('run_started', {'run_id': log.run_id})  # so a channel can link to the run while it goes
    gate = None

    def end(outcome: str, **extra):
        log.append(ev.RUN_END, outcome=outcome, duration_ms=int((time.monotonic() - started) * 1000),
                   turns=views.turns(log.events), tokens=views.tokens_used(log.events),
                   stop_reason=views.stop_reason(log.events), **extra)

    try:
        # The daily budget is checked before any model call is paid for
        policy = rt.policy(budget)
        if policy.over_daily_budget():
            log.append(ev.STOP, reason='daily_budget', message='The daily usage limit was reached.')
            end('refused')
            return RunResult(DAILY_BUDGET_MESSAGE, log.run_id, 'refused')

        if builtins:
            given = {tool.name for tool in tools}
            tools = [tool for tool in builtin_tools(rt, owner=owner, attended=callback is not None)
                     if tool.name not in given] + list(tools)
        if mcp is not None:
            app_tools = [*app_tools, *mcp.tools]
            app_guides = {**mcp.guides, **(app_guides or {})}
        if allowed_tools is not None:  # before the skills list, which depends on the tools offered
            tools = [tool for tool in tools if tool.name in allowed_tools]
            app_tools = [tool for tool in app_tools if tool.name in allowed_tools]
        orders = standing_orders(rt.home)
        gate = PermissionGate(rt.store, callback, history, '' if untrusted_source else message, instructions=orders,
                              owner_email=rt.config.owner_email, tool_access=mcp.access if mcp else None,
                              preapproved=mcp.always_allowed if mcp else (), always_ask=mcp.destructive if mcp else (),
                              channel=source, run_id=log.run_id)
        if untrusted_source:
            gate.add_untrusted_input(untrusted_source, message)
        for source, text in inherited_taint:
            gate.add_untrusted_input(source, text)
        notes = rt.memory.load() if owner else []

        # Skills: one line each in the prompt, instructions on skill_load. The owner's own skills and rules only
        # for the owner, and only the owner can change them.
        offered_names = [tool.name for tool in [*tools, *app_tools]]
        skills = registry.available_skills(rt.skills.user_skills() if owner else [],
                                           registry.capabilities(offered_names, owner, mcp.provides if mcp else ()),
                                           rt.skills.adjustments() if owner else {})
        situation = reminders.Situation(
            message=message, events=log.events, owner=owner, skills=skills,
            previous_run=reminders.previous_run_events(rt.store, log.run_id) if owner else [],
            history_summarized=compaction.is_summarized(history),
            context_window=reminders.context_window_of(rt.model_for(purpose)))

        def after_result(tool_name, result_text):
            situation.tool, situation.tool_output = tool_name, result_text
            return reminders.inject(log, tool_name, reminders.due(reminders.REMINDERS, tool_name, situation))

        own = memory_tools.memory_tools(rt.memory, gate, log) if owner else []
        (load_tool, propose_tool, adjust_tool), prechecks = skill_tools(rt.skills, skills, gate, log)
        own += [load_tool] if skills else []
        own += [propose_tool, adjust_tool] if owner else []
        # Tools that need this run's gate or event log. Each worker adds its own, inside its block only.
        # >>> security
        # <<< security
        # >>> efficiency
        # <<< efficiency
        # >>> capabilities
        # <<< capabilities
        # >>> reach
        # <<< reach
        # >>> evals
        # <<< evals
        # >>> sandbox
        # <<< sandbox
        if allowed_tools is not None:
            own = [tool for tool in own if tool.name in allowed_tools]
        result_hooks = tool_results.result_hooks(rt, log)

        use_discovery = len(app_tools) > disclosure.DIRECT_ACTIONS_LIMIT
        untrusted_context = []  # third-party text in what the model is given before any tool runs
        if use_discovery:
            # App actions stay out of the model's tool list: run_app_action calls their guarded versions
            catalog = disclosure.AppCatalog(guard_function_tools(list(app_tools), gate, log, after_result,
                                                                 result_hooks=result_hooks),
                                            guides=app_guides, third_party=mcp.origin if mcp else ())
            find_tool, describe_tool, run_tool = disclosure.discovery_tools(catalog, log, label=gate.label)
            guarded = guard_function_tools([*tools, *own, find_tool, describe_tool], gate, log, after_result,
                                           prechecks, result_hooks) + [run_tool]
        else:
            guarded = guard_function_tools([*tools, *app_tools, *own], gate, log, after_result, prechecks,
                                           result_hooks)
            if mcp is not None and mcp.tools:
                # The servers' own descriptions are in the model's tool list from the first step: third-party text
                gate.add_untrusted_input(MCP_DESCRIPTIONS, '\n'.join(
                    [tool.description or '' for tool in mcp.tools] + list(mcp.guides.values())), private=False)
                untrusted_context.append(MCP_DESCRIPTIONS)

        now = datetime.now().astimezone()
        instructions = BASE_INSTRUCTIONS.format(now=f'{now:%A %d %B %Y, %H:%M} ({now.tzname()})')
        if orders:
            instructions += f'\nSTANDING ORDERS (from the user, in every conversation):\n{orders}\n'
        if owner:
            instructions += vault.memory_context(notes)  # L0 (profile) and L1 (keyword map); the rest on demand
        instructions += registry.registry_prompt(skills, can_adjust=owner)
        instructions += reminders.PREAMBLE
        if use_discovery:
            instructions += disclosure.discovery_instructions(sorted({disclosure.app_of(t.name) for t in app_tools}),
                                                              len(app_tools))

        model = rt.model_for(purpose)
        # What the model starts with: the size of this is what progressive disclosure shrinks
        log.append(ev.CONTEXT_BUILT, model=str(getattr(model, 'model', model)),
                   disclosure='discovery' if use_discovery else 'direct', app_actions=len(app_tools),
                   tools_offered=[tool.name for tool in guarded], tool_schema_chars=disclosure.schema_chars(guarded),
                   memory_notes=len(notes) if owner else None, skills_offered=[skill.name for skill in skills],
                   instructions_chars=len(instructions), untrusted_context=untrusted_context,
                   **({'mcp_servers': sorted({server for server, _ in mcp.origin.values()}),
                       'mcp_errors': mcp.errors} if mcp else {}))

        chat = history + [{'role': 'user', 'content': user_content(message, images)}]
        # Reminders for this moment go right after the owner's message
        start_block = reminders.inject(log, reminders.RUN_START,
                                       reminders.due(reminders.REMINDERS, reminders.RUN_START, situation))
        if start_block:
            chat.append({'role': 'user', 'content': start_block})

        # include_usage: streaming through LiteLLM (Anthropic) reports token usage only when asked, and the budgets
        # count what's reported
        agent = Agent(name='Local Agent', instructions=instructions, tools=guarded, model=model,
                      model_settings=ModelSettings(include_usage=True))
        result = await _run_loop(agent, chat, callback, log, policy, _strong_upgrade(rt, skills))
        answer = str(result.final_output or '')
        end('answered')
        return RunResult(answer, log.run_id, 'answered')
    except Exception as e:
        logger.error(f'Run {log.run_id} failed: {type(e).__name__}: {e}')
        end('failed', error=type(e).__name__)
        # Honest answer from what actually ran. No fallback to a second, tool-less model: it had no memory of
        # the run and could claim actions that never happened. Details stay in the event log.
        return RunResult(honest_failure_message(gate.completed_writes() if gate else [],
                                                timed_out=isinstance(e, TimeoutError)), log.run_id, 'failed')
    finally:
        log.save()
