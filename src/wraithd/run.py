"""
One run: build the context, run the loop, log every step, return the answer.

This is agent-studio's 2,500-line agent_service.run() reduced to what the harness needs: the owner's standing
orders, memory (L0 + L1) and the skills list in the prompt; memory and skill tools behind the permission gate;
reminders; the loop with its stop rules; and an event log that is saved even when the run fails.

The system prompt and the tool list are a stable prefix: the same bytes on every turn and every run of a
conversation, so providers can cache them (docs/caching.md). Anything that changes per run or per turn (the time,
reminders) goes after the owner's message or in tool results.
"""

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime

from agents import Agent, Model, Runner, set_tracing_disabled

from wraithd.config import Config, Home, load_config
from wraithd.core import compaction, disclosure, reminders, tokens, tool_results, views
from wraithd.core import events as ev
from wraithd.core.gate import PermissionGate, guard_function_tools
from wraithd.core.loop import RunAlreadyStartedError, run_with_step_limit
from wraithd.core.stop import DAILY_BUDGET_MESSAGE, StopPolicy, final_answer_instruction, tokens_used_today
from wraithd.engines import claude_code
from wraithd.mcp.client import Connected, McpConnections
from wraithd.memory import tools as memory_tools
from wraithd.memory import vault
from wraithd.memory.notes import Vault
from wraithd.providers import engine_models, model_for, model_settings
from wraithd.skills import registry
from wraithd.skills.tools import skill_tools
from wraithd.store import SQLiteStore, Store
from wraithd.tools import builtin_tools
from wraithd.tools.notify import StoreNotifier

logger = logging.getLogger(__name__)

RUN_TIMEOUT_SECONDS = 180
RETRY_DELAYS = (1, 2)  # seconds before the 2nd and 3rd attempt; only for failures before anything happened
INSTRUCTIONS_CHARS = 10_000  # cap on AGENTS.md in the prompt
MCP_DESCRIPTIONS = 'MCP tool descriptions'  # the taint source when MCP tools are given to the model directly

BASE_INSTRUCTIONS = (
    'You are wraithd, a personal assistant that runs on the user\'s own computer. Be direct and useful. '
    'Use your tools when they help; never claim you did something a tool didn\'t do. '
    'When there is one right next step or question, end with it; never a generic "anything else?".\n'
)
TELEGRAM_PAIRED = 'telegram.owner_chat'  # channels/telegram.OWNER_CHAT (not imported: channels import this module)


def setup_line(rt: 'Runtime') -> str:
    """
    One line on what the agent runs on and which channels are on, so "are you on my Claude subscription?" gets a
    true answer. It's in the stable prefix, so it holds only what is fixed for a conversation: the engine, the
    configured models (never a key) and whether Telegram is paired.
    """
    engine, model = rt.engine, rt.config.model
    if engine['name'] == 'claude-code':
        on = (f'Claude Code on the user\'s Claude subscription (no API key): {engine["chat_model"]} for chats, '
              f'{engine["background_model"]} for background work')
        if rt.models:
            on += f'; pictures: {model.provider} API, {model.default}'
    elif model.provider == 'ollama':
        on = f'a local model (Ollama, {model.default}), no subscription or API key'
    else:
        strong = f' ({model.strong} for harder skills)' if model.strong and model.strong != model.default else ''
        on = f'the {model.provider} API on the user\'s own key, paid per use, no subscription: {model.default}{strong}'
    telegram = 'paired' if rt.store.get_setting(TELEGRAM_PAIRED) else 'off'
    return f'You run on {on}. Telegram: {telegram}.\n'
# Claude Code engine, a picture and no API model to read it
NO_IMAGES_MESSAGE = "I can't read images yet on your Claude subscription. Send me the text instead."
# The time changes every minute, so it goes after the owner's message, never in the cached prefix
NOW_NOTE = 'Now: {now}.'


@dataclass
class Runtime:
    """Everything a run needs, opened once per process: the state folder, config, store, models."""

    home: Home
    config: Config
    store: Store
    models: dict[str, Model]  # 'default', and 'strong' if one is configured; {} on claude-code without a key
    # The web tools' HTTP transports: None is the real network; tests pass httpx.MockTransport
    web_transport: object = None
    search_transport: object = None
    voice_transport: object = None  # the transcription provider's (voice.py)
    notifier: object = None  # where notify_owner delivers; the daemon sets Telegram, else the store's inbox
    memory: Vault = field(init=False)
    skills: registry.SkillFolder = field(init=False)
    sessions: claude_code.SessionPool = field(init=False)  # warm Claude Code sessions, by conversation id

    def __post_init__(self):
        # The Agents SDK uploads run traces to OpenAI by default (whenever OPENAI_API_KEY is in the environment).
        # Runs on your own machine stay local: every entry point (chat, daemon, schedules, evals) builds a Runtime.
        set_tracing_disabled(True)
        self.notifier = self.notifier or StoreNotifier(self.store)
        self.memory = Vault(self.home.memory_dir)
        self.skills = registry.SkillFolder(self.home.skills_dir)
        self.sessions = claude_code.SessionPool(self.engine['idle_minutes'])

    @classmethod
    def open(cls, home: Home | None = None, models: dict[str, Model] | None = None) -> 'Runtime':
        home = home or Home.locate()
        home.ensure()
        config = load_config(home)
        return cls(home=home, config=config, store=SQLiteStore(home.db_path), models=models or engine_models(config))

    @property
    def engine(self) -> dict:
        """[engine]: which engine thinks (agents-sdk or claude-code), and its settings."""
        return self.config.section('engine')

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
        if self.engine['name'] == 'claude-code':
            try:
                return await asyncio.wait_for(claude_code.complete(
                    prompt, claude_code.model_for(self.engine, purpose)), timeout=60)
            except claude_code.SubscriptionUnavailable:
                if not (self.engine['fallback'] and self.models):  # nothing ran yet, so falling back is safe
                    raise
        helper = Agent(name='helper', instructions='Follow the request exactly.', model=self.model_for(purpose))
        result = await asyncio.wait_for(Runner.run(helper, prompt, max_turns=1), timeout=60)
        return str(result.final_output or '')

    def completer(self, purpose: str):
        """complete() for one purpose, as the complete(prompt) callable compaction and distill take."""
        async def complete(prompt: str) -> str:
            return await self.complete(prompt, purpose)
        return complete

    async def close_sessions(self) -> None:
        """Closes the warm Claude Code sessions (they also close when the event loop running them ends)."""
        await self.sessions.close()

    def close(self) -> None:
        self.sessions.close_soon()
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


def subscription_message(problem: str, completed_writes: list[str]) -> str:
    """What the owner is told when the Claude login can't be used or the plan's limit hit, and there's no fallback."""
    if completed_writes:
        done = '\n'.join(f'- {action}' for action in completed_writes)
        return (f'{problem} I stopped here. These actions were already done:\n{done}\n'
                'Check them before asking me to repeat anything.')
    later = ' It resets after a while: try again then.' if problem == claude_code.LIMIT_MESSAGE else ''
    return f'{problem} Nothing was changed.{later}'


def standing_orders(home: Home) -> str:
    """AGENTS.md, the owner's own words: in every run, and what the gate counts as written by the owner."""
    try:
        text = home.agents_md.read_text().strip()
    except FileNotFoundError:
        return ''
    return text[:INSTRUCTIONS_CHARS]


def _strong_upgrade(rt: Runtime, skills: list[registry.SkillSpec], metered=lambda model: model):
    """The loop's upgrade check: once a `model: strong` skill has loaded, switch to the strong model."""
    strong = {skill.name for skill in skills if skill.model == 'strong'}
    if rt.model_for('strong') is rt.model_for('chat') or not strong:
        return None

    def upgrade(events):
        loaded = next((name for name in views.skills_loaded(events) if name in strong), None)
        return (metered(rt.model_for('strong')), loaded) if loaded else None

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


UNATTENDED_SOURCES = ('schedule', 'watch')  # scheduled runs and feed-watch runs: both on the schedule's model


def engine_purpose(purpose: str, source: str) -> str:
    """What picks a run's Claude Code model: its purpose, or 'schedule' for a scheduled or feed-watch run."""
    return 'schedule' if source in UNATTENDED_SOURCES else purpose


def claude_model(engine: dict, purpose: str, source: str, message: str, skills) -> tuple[str, str | None]:
    """
    A run's Claude Code model, picked before it starts (Claude Code can't switch mid-run like the API engine's
    _strong_upgrade), and why, for a scheduled run. A scheduled run whose message names a `model: strong` skill runs
    on chat_model; any other on schedule_model (background_model unless set). Nothing else moves it up.
    """
    chosen = engine_purpose(purpose, source)
    if chosen != 'schedule':
        return claude_code.model_for(engine, chosen), None
    strong = registry.strong_skill_named(message, skills)
    if strong:
        return engine['chat_model'], f'skill {strong} is model: strong'
    model = claude_code.model_for(engine, chosen)
    return model, 'schedule_model' if (engine.get('schedule_model') or '').strip() else 'background_model'


def warm_eligible(rt: Runtime, conversation: str | None, purpose: str, source: str) -> bool:
    """A chat in a conversation, on Claude Code: it keeps one warm session per conversation."""
    return (bool(conversation) and rt.engine['name'] == 'claude-code'
            and engine_purpose(purpose, source) not in claude_code.BACKGROUND and purpose in ('chat', 'strong'))


async def _run_claude(rt, log, gate, policy, instructions, chat, guarded, callback, model, lease=None,
                      new_from: int = 0):
    """
    The run on Claude Code (engines/claude_code.py): (answer, outcome, run_end extras), or None to fall back to the
    Agents SDK engine. Fallback happens only when the login can't be used or the plan's limit hit, [engine] fallback
    is on, an API model exists, no write has happened and the owner saw no text: the same rule as
    RunAlreadyStartedError, so nothing (an email) is ever done twice.
    """
    def stop():
        """The run's stop rules (StopPolicy), checked before every tool call: the Agents SDK engine's run hooks."""
        reason = policy.check(log.events)
        if reason is None:
            return None
        if views.stop_reason(log.events) != reason.code:
            log.append(ev.STOP, reason=reason.code, message=reason.message)
        return final_answer_instruction(reason)

    progress = {'streamed': False}
    options = {'limit': policy.max_tool_calls, 'stop': stop, 'callback': callback, 'log': log, 'progress': progress}
    try:
        if lease is not None:
            # A warm session: a new one gets the whole history as a transcript, a known one only this run's messages
            session = await lease.session(instructions, guarded, model)
            text = claude_code.transcript(chat if session.fresh else chat[new_from:])
            call = lease.ask(session, text, guarded, untrusted_texts=lambda: gate.untrusted_texts, **options)
        else:
            call = claude_code.run_once(instructions, claude_code.transcript(chat), guarded, model, **options)
        answer, result = await asyncio.wait_for(call, timeout=RUN_TIMEOUT_SECONDS)
    except claude_code.SubscriptionUnavailable as e:
        writes = gate.completed_writes()
        if writes or progress['streamed'] or not rt.engine['fallback'] or not rt.models:
            log.append(ev.STOP, reason='subscription_unavailable', message=str(e))
            return subscription_message(str(e), writes), 'refused', {'engine': 'claude-code'}
        log.append(ev.STOP, reason='engine_fallback', message=str(e))
        return None
    return answer, 'answered', {'engine': 'claude-code', 'claude_session': result.session_id if result else None}


async def run(rt: Runtime, message: str, *, history=(), callback=None, source: str = 'chat', owner: bool = True,
              tools=(), app_tools=(), app_guides: dict[str, str] | None = None, builtins: bool = True,
              mcp: Connected | None = None, untrusted_source: str = '', images=(), parent_run_id: str | None = None,
              inherited_taint=(), budget: dict | None = None, purpose: str = 'chat',
              allowed_tools=None, conversation: str | None = None, system_prompt: str | None = None,
              warm_up: bool = False) -> RunResult:
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
    conversation: the conversation's id. On the Claude Code engine a chat keeps one warm Claude Code session per
    conversation (engines/claude_code.py); third-party text its earlier messages read taints this one too.
    system_prompt: a short prompt for a tool-less run (the lean heartbeat): it replaces the base instructions, the
    reminders and the skills list; the standing orders and the memory profile stay.
    warm_up: start_warm_session(): build the context, start its warm session, and return; no run happens.
    """
    async with AsyncExitStack() as stack:
        lease = None
        if warm_eligible(rt, conversation, purpose, source):
            # Held until the answer, from before the gate is built: earlier messages' taint is this run's too
            lease = await stack.enter_async_context(rt.sessions.conversation(conversation))
            inherited_taint = (*inherited_taint, *(pair for pair in lease.taint if pair not in inherited_taint))
        if owner and mcp is None and rt.config.mcp_servers:
            mcp = await stack.enter_async_context(McpConnections(rt.config.mcp_servers, store=rt.store))
        return await _run(rt, message, history=history, callback=callback, source=source, owner=owner, tools=tools,
                          app_tools=app_tools, app_guides=app_guides, builtins=builtins,
                          mcp=mcp if owner else None, untrusted_source=untrusted_source, images=images,
                          parent_run_id=parent_run_id, inherited_taint=tuple(inherited_taint), budget=budget,
                          purpose=purpose, allowed_tools=None if allowed_tools is None else set(allowed_tools),
                          stack=stack, lease=lease, system_prompt=system_prompt, warm_up=warm_up)


async def start_warm_session(rt: Runtime, conversation: str, *, mcp: Connected | None = None) -> bool:
    """
    Starts a chat conversation's warm Claude Code session before its first message (`wraithd chat` as it opens), so
    Claude Code starts while the owner types. Claude Code's tools, system prompt and model are fixed when it connects,
    so they are built exactly as that message's run() builds them; anything else would open a second session. The
    first message waits for it (the conversation's lock). False when there's nothing to start or it couldn't: the
    first message then starts it as it always did. Cancel it to stop it (the session is closed).
    """
    if not warm_eligible(rt, conversation, 'chat', 'chat'):
        return False

    async def attended(event_type: str, data: dict) -> None:  # a chat is attended: the same tools as its runs
        return None

    # True only when started: a refusal before the context is built (the daily budget) is a RunResult, not a start
    return await run(rt, '', callback=attended, source='chat', mcp=mcp, conversation=conversation, warm_up=True) is True


def user_content(message: str, images=()):
    """The owner's message as model input: plain text, or text and images (the SDK turns input_image into each
    provider's own image part, through LiteLLM too)."""
    if not images:
        return message
    return [{'type': 'input_text', 'text': message},
            *({'type': 'input_image', 'image_url': image, 'detail': 'auto'} for image in images)]


async def _run(rt, message, *, history, callback, source, owner, tools, app_tools, app_guides, builtins, mcp,
               untrusted_source, images, parent_run_id, inherited_taint, budget, purpose, allowed_tools, stack,
               lease=None, system_prompt=None, warm_up=False):
    started = time.monotonic()
    history = list(history)
    log = ev.EventLog(None if warm_up else rt.store)  # a warm-up is no run: nothing of it is saved
    log.append(ev.RUN_START, source=source, message=message, history_messages=len(history), owner=owner,
               **({'images': len(images)} if images else {}),
               untrusted_inputs=[*([untrusted_source] if untrusted_source else []),
                                 *(source for source, _ in inherited_taint)],
               parent_run_id=parent_run_id, **({'purpose': purpose} if purpose != 'chat' else {}))
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
        engine = rt.engine
        use_claude = engine['name'] == 'claude-code' and not (images and rt.models)  # images stay on the API
        if engine['name'] == 'claude-code' and images and not rt.models:
            log.append(ev.STOP, reason='images', message='No model that can read images is set up.')
            end('refused')
            return RunResult(NO_IMAGES_MESSAGE, log.run_id, 'refused')

        if builtins:
            given = {tool.name for tool in tools}
            tools = [tool for tool in builtin_tools(rt, owner=owner, attended=callback is not None)
                     if tool.name not in given] + list(tools)
            if not rt.config.web_read:  # [web] read = false: no page reading at all, not even asked about
                tools = [tool for tool in tools if tool.name != 'read_website']
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
        user_skills = rt.skills.user_skills() if owner else []
        can_use = registry.capabilities(offered_names, owner, mcp.provides if mcp else ())
        skills = registry.available_skills(user_skills, can_use, rt.skills.adjustments() if owner else {})
        # The owner hears which skills are off and what turns each on (not in a run limited to some tools)
        hidden = registry.hidden_skills(user_skills, can_use) if owner and allowed_tools is None else []
        engine_model, model_reason = claude_model(engine, purpose, source, message, skills) if use_claude \
            else (None, None)
        situation = reminders.Situation(
            message=message, events=log.events, owner=owner, skills=skills,
            previous_run=reminders.previous_run_events(rt.store, log.run_id) if owner else [],
            history_summarized=compaction.is_summarized(history),
            context_window=reminders.context_window_of(engine_model or rt.model_for(purpose)))

        def after_result(tool_name, result_text):
            situation.tool, situation.tool_output = tool_name, result_text
            return reminders.inject(log, tool_name, reminders.due(reminders.REMINDERS, tool_name, situation))

        own = memory_tools.memory_tools(rt.memory, gate, log, notifier=rt.notifier) if owner else []
        (load_tool, propose_tool, adjust_tool), prechecks = skill_tools(rt.skills, skills, gate, log)
        own += [load_tool] if skills else []
        own += [propose_tool, adjust_tool] if owner else []
        # Tools that need this run's gate or event log. Each worker adds its own, inside its block only.
        # >>> security
        gate.trust_owner_skills(skills)  # links in the owner's own skill files count as the owner's (gate.py)
        # <<< security
        # >>> efficiency
        # <<< efficiency
        # >>> capabilities
        from wraithd.core import delegate
        from wraithd.tools import browser

        own += browser.run_tools(rt, log, owner, stack)  # closed when the run ends (stack)
        own += delegate.delegate_tools(rt, gate, log, callback=callback, owner=owner, mcp=mcp,
                                       parent_run_id=parent_run_id, tools=tools, app_tools=app_tools, own=own,
                                       allowed_tools=allowed_tools)
        # <<< capabilities
        # >>> reach
        # <<< reach
        # >>> evals
        # <<< evals
        # >>> sandbox
        from wraithd import sandbox

        sandbox.bind_log(log, stack)
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

        # The stable prefix, most stable first: it must be byte-identical on every turn and every run of a
        # conversation (docs/caching.md). Memory comes last: distill changes it more often than the rest.
        sections = {tokens.AGENTS_MD: f'\nSTANDING ORDERS (from the user, in every conversation):\n{orders}\n'
                                      if orders else '',
                    tokens.SKILLS: registry.registry_prompt(skills, can_adjust=owner, hidden=hidden),
                    tokens.PROFILE: vault.memory_context(notes) if owner else ''}  # L0 + L1; the rest on demand
        if system_prompt is not None:  # a tool-less run's short prompt: no skills list, no reminders
            sections[tokens.SKILLS] = ''
            instructions = system_prompt + sections[tokens.AGENTS_MD]
        else:
            instructions = (BASE_INSTRUCTIONS + setup_line(rt) + reminders.PREAMBLE + sections[tokens.AGENTS_MD]
                            + sections[tokens.SKILLS])
        if use_discovery:
            instructions += disclosure.discovery_instructions(sorted({disclosure.app_of(t.name) for t in app_tools}),
                                                              len(app_tools))
        instructions += sections[tokens.PROFILE]
        if warm_up:
            return bool(use_claude and lease) and await lease.warm(instructions, guarded, engine_model)

        model = engine_model or rt.model_for(purpose)
        # What the model starts with: the size of this is what progressive disclosure shrinks
        log.append(ev.CONTEXT_BUILT, model=str(getattr(model, 'model', model)),
                   **({'engine': 'claude-code'} if use_claude else {}),
                   **({'model_reason': model_reason} if model_reason else {}),
                   disclosure='discovery' if use_discovery else 'direct', app_actions=len(app_tools),
                   tools_offered=[tool.name for tool in guarded], tool_schema_chars=disclosure.schema_chars(guarded),
                   memory_notes=len(notes) if owner else None, skills_offered=[skill.name for skill in skills],
                   instructions_chars=len(instructions), untrusted_context=untrusted_context,
                   prefix_hash=tokens.prefix_hash(instructions, guarded),
                   section_chars={name: len(text) for name, text in sections.items()},
                   **({'mcp_servers': sorted({server for server, _ in mcp.origin.values()}),
                       'mcp_errors': mcp.errors} if mcp else {}))

        # After the owner's message, outside the cached prefix: the time (the harness's words, so role system), then
        # the reminders for this moment
        now = datetime.now().astimezone()
        time_note = NOW_NOTE.format(now=f'{now:%A %d %B %Y, %H:%M} ({now.tzname()})')
        chat = history + [{'role': 'user', 'content': user_content(message, images)},
                          {'role': 'system', 'content': time_note}]
        start_block = None if system_prompt is not None else reminders.inject(
            log, reminders.RUN_START, reminders.due(reminders.REMINDERS, reminders.RUN_START, situation))
        if start_block:
            chat.append({'role': 'user', 'content': start_block})

        if use_claude:
            outcome = await _run_claude(rt, log, gate, policy, instructions, chat, guarded, callback, engine_model,
                                        lease, new_from=len(history))
            if outcome is not None:
                answer, status, extra = outcome
                end(status, **extra)
                return RunResult(answer, log.run_id, status)
            model = rt.model_for(purpose)  # fell back: the rest is the Agents SDK engine on [model]

        def metered(chosen):
            return tokens.TokenMeter(chosen, log, sections)

        agent = Agent(name='wraithd', instructions=instructions, tools=guarded, model=metered(model),
                      model_settings=model_settings(rt.config))
        result = await _run_loop(agent, chat, callback, log, policy, _strong_upgrade(rt, skills, metered))
        answer = str(result.final_output or '')
        end('answered')
        return RunResult(answer, log.run_id, 'answered')
    except Exception as e:
        if warm_up:  # the first message builds it again, and says so if it fails then
            logger.info(f'Could not start the warm session early: {type(e).__name__}: {e}')
            return False
        logger.error(f'Run {log.run_id} failed: {type(e).__name__}: {e}')
        end('failed', error=type(e).__name__)
        # Honest answer from what actually ran. No fallback to a second, tool-less model: it had no memory of
        # the run and could claim actions that never happened. Details stay in the event log.
        return RunResult(honest_failure_message(gate.completed_writes() if gate else [],
                                                timed_out=isinstance(e, TimeoutError)), log.run_id, 'failed')
    finally:
        log.save()
