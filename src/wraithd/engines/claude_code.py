"""
The Claude Code engine: Anthropic's unmodified Claude Code program runs the thinking loop, signed in with the owner's
own Claude Pro or Max login (claude_options.py has the rules). Claude Code sees only wraithd's guarded tools
(claude_bridge.py), so every action still goes through the gate.

Two ways to run:
- run_once(): one Claude Code process for one run. Background work (heartbeat, schedules), rt.complete() (memory
  distill, compaction) and any run without a conversation.
- WarmSession: one Claude Code process per conversation, kept between messages so only the first one pays the
  ~4.6s start. SessionPool (owned by the Runtime) keys them by conversation id and closes idle ones. `wraithd chat`
  starts its conversation's session as soon as it opens (run.warm_up), so the start overlaps the owner's typing.

wraithd stays the source of truth: its (compacted) history goes to a new session as a transcript, and its event
log records every tool call and the tokens used, so the budgets count.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping

from wraithd.engines import claude_options as options_module
from wraithd.engines.claude_bridge import RunSlot, bridge, tool_names
from wraithd.engines.claude_options import BACKGROUND, LOGIN, SubscriptionUnavailable, claude_options, model_for, sdk

__all__ = ['BACKGROUND', 'SubscriptionUnavailable', 'run_once', 'complete', 'WarmSession', 'SessionPool', 'model_for']

logger = logging.getLogger(__name__)

LIMIT_MESSAGE = 'Your Claude plan\'s usage limit was reached.'
NOT_LOGIN = 'Claude Code is not using your Claude login ({source}): run `claude` and type /login.'
# Not signed in at all: init still says apiKeySource "none", and the failure comes with the first model call
NOT_SIGNED_IN = "Claude Code isn't signed in: run `claude` and type /login."
AUTH_STATUSES = (401, 403)
HELPER_PROMPT = 'Follow the request exactly.'
REAP_EVERY_SECONDS = 60
CLOSE_WAIT_SECONDS = 5  # how long close() waits for a message that is answering to stop


class EngineError(Exception):
    """Claude Code ended the run with an error that isn't the plan's limit."""


def transcript(chat) -> str:
    """wraithd's history (or a run's new messages) as text. Images are left out."""
    return '\n\n'.join(f'{m["role"].upper()}: {m["content"]}' for m in chat if isinstance(m.get('content'), str))


def usage_numbers(usage: Mapping | None) -> dict[str, int]:
    """Claude Code's usage as the event log counts it: input includes cache reads and writes (as LiteLLM's does)."""
    usage = usage or {}
    cache_read = usage.get('cache_read_input_tokens') or 0
    cache_write = usage.get('cache_creation_input_tokens') or 0
    return {'input_tokens': (usage.get('input_tokens') or 0) + cache_read + cache_write,
            'output_tokens': usage.get('output_tokens') or 0, 'cache_read': cache_read, 'cache_write': cache_write}


def _ready() -> None:
    problem = options_module.subscription_env_problem()
    if problem:
        raise SubscriptionUnavailable(problem)


def _limit_hit(claude, message) -> bool:
    """The plan's limit, as Claude Code reports it before the result: a rate_limit reply or a rejected window."""
    if isinstance(message, claude.AssistantMessage):
        return message.error == 'rate_limit'
    if isinstance(message, getattr(claude, 'RateLimitEvent', ())):
        return message.rate_limit_info.status == 'rejected'
    return False


def _connected(log, started: float, kind: str, **extra) -> None:
    """engine_connect: how long Claude Code took to start (run_end's duration_ms doesn't show it apart)."""
    from wraithd.core import events as ev

    if log is not None:
        log.append(ev.ENGINE_CONNECT, engine='claude-code', kind=kind, ms=int((time.monotonic() - started) * 1000),
                   **extra)


async def _consume(claude, messages, callback=None, log=None, progress: dict | None = None,
                   started: float | None = None, kind: str = 'once', connect_extra: dict | None = None):
    """
    (answer, ResultMessage) from one run's messages. The login is checked on init, before any model call; the plan's
    limit raises SubscriptionUnavailable. Text goes to callback as content_delta events; progress['streamed'] turns
    True on the first. Each model call is a turn_start in log, and the result's usage a model_output. started: when
    Claude Code was asked to start (a one-shot's query, a warm session's connect); its init, or the first answer
    when no init comes, is then logged as engine_connect of this kind (with connect_extra's fields).
    """
    from wraithd.core import events as ev

    progress = progress if progress is not None else {}
    progress.setdefault('streamed', False)
    index, turns = 0, set()
    not_found = getattr(claude, 'CLINotFoundError', ())
    try:
        async for message in messages:
            if started is not None and (isinstance(message, (claude.AssistantMessage, claude.StreamEvent,
                                                             claude.ResultMessage))
                                        or (isinstance(message, claude.SystemMessage) and message.subtype == 'init')):
                _connected(log, started, kind, **(connect_extra or {}))
                started = None
            if isinstance(message, claude.SystemMessage) and message.subtype == 'init':
                source = message.data.get('apiKeySource')
                if source != LOGIN:
                    raise SubscriptionUnavailable(NOT_LOGIN.format(source=source or 'no login'))
            elif _limit_hit(claude, message):
                raise SubscriptionUnavailable(LIMIT_MESSAGE)
            elif isinstance(message, claude.AssistantMessage) and message.error == 'authentication_failed':
                raise SubscriptionUnavailable(NOT_SIGNED_IN)
            elif isinstance(message, claude.StreamEvent):
                delta = message.event.get('delta') or {}
                if delta.get('type') == 'text_delta' and delta.get('text'):
                    progress['streamed'] = True
                    if callback:
                        await callback('content_delta', {'content': delta['text'], 'index': index})
                        index += 1
            elif isinstance(message, claude.AssistantMessage):
                turn = message.message_id or id(message)
                if log is not None and turn not in turns:
                    turns.add(turn)
                    log.append(ev.TURN_START, turn=len(turns), engine='claude-code', model=message.model)
            elif isinstance(message, claude.ResultMessage):
                if log is not None:
                    log.append(ev.MODEL_OUTPUT, engine='claude-code', **usage_numbers(message.usage),
                               num_turns=message.num_turns, text_chars=len(message.result or ''))
                if message.is_error:
                    if message.api_error_status == 429:
                        raise SubscriptionUnavailable(LIMIT_MESSAGE)
                    if message.api_error_status in AUTH_STATUSES:
                        raise SubscriptionUnavailable(NOT_SIGNED_IN)
                    raise EngineError(f'Claude Code stopped: {message.subtype} {message.errors or ""}'.strip())
                return message.result or '', message
    except not_found as e:
        raise SubscriptionUnavailable(f'Claude Code could not start: {e}') from e
    return '', None


async def run_once(instructions: str, prompt: str, guarded, model: str, callback=None, *, limit: int | None = None,
                   stop=None, log=None, progress: dict | None = None):
    """One run in its own Claude Code process: (answer, ResultMessage or None). guarded: the run's tools after
    guard_function_tools() ([] for a tool-less call); model: [engine] model_for() this purpose."""
    _ready()
    claude = sdk()
    slot = RunSlot()
    server, names = bridge(guarded, slot) if guarded else (None, [])
    slot.attach(guarded, limit, stop)
    options = claude_options(instructions, server, names, model, stream=callback is not None)
    try:
        # Closed here, in order, before returning: left open, the event loop closes the SDK's nested generators
        # all at once when it ends, and one of them is still closing ("aclose(): ... already running").
        async with contextlib.aclosing(claude.query(prompt=prompt, options=options)) as messages:
            return await _consume(claude, messages, callback, log, progress, started=time.monotonic())
    finally:
        slot.detach()


async def complete(prompt: str, model: str) -> str:
    """One tool-less call, for the harness's own work (memory distill, compaction, summaries)."""
    answer, _ = await run_once(HELPER_PROMPT, prompt, [], model)
    return answer


class WarmSession:
    """One Claude Code process per conversation, reused between messages. Its tools, system prompt and model are
    fixed when it connects: a run that needs others gets a new session (SessionPool)."""

    def __init__(self, instructions: str, guarded, model: str):
        claude = sdk()
        self.slot = RunSlot()
        server, self.names = bridge(guarded, self.slot)
        self.key = (tuple(self.names), instructions, model)
        self.client = claude.ClaudeSDKClient(options=claude_options(instructions, server, self.names, model,
                                                                    stream=True))
        self.lock = asyncio.Lock()
        self.connected, self.fresh, self.last_used = False, True, time.monotonic()
        self.answering: asyncio.Task | None = None  # the task in ask() or start(), while it runs
        self.early_seconds: float | None = None  # how long start()'s connect took, until the first message logs it

    async def start(self) -> None:
        """Connects now, before any message (`wraithd chat` as it opens), so Claude Code starts while the owner
        types. A message that comes first waits for it (the lock). Its time is logged by the first message."""
        async with self.lock:
            if self.connected:
                return
            self.answering = asyncio.current_task()
            try:
                _ready()
                started = time.monotonic()
                await self.client.connect()
                self.connected, self.early_seconds = True, time.monotonic() - started
            finally:
                self.answering = None
                self.last_used = time.monotonic()

    def fits(self, instructions: str, guarded, model: str) -> bool:
        return self.key == (tuple(tool_names(guarded)), instructions, model)

    async def ask(self, text: str, guarded, *, limit=None, stop=None, callback=None, log=None, progress=None):
        """One message: the run's guarded tools in the slot for exactly as long as it runs. Messages wait their
        turn (the lock), so two never share a slot."""
        async with self.lock:
            self.answering = asyncio.current_task()
            claude = sdk()
            started, extra = None, None
            if self.early_seconds is not None:
                # Started early: engine_connect is still its real time, start()'s connect plus the wait for init,
                # not the minutes the owner spent typing in between
                started, extra = time.monotonic() - self.early_seconds, {'early': True}
                self.early_seconds = None
            elif not self.connected:
                _ready()
                # Ready at Claude Code's init (or first answer) on this first message, not when connect() returns
                started = time.monotonic()
                await self.client.connect()
                self.connected = True
            self.slot.attach(guarded, limit, stop)
            try:
                await self.client.query(text)
                # Closed before the lock is released, so close() never disconnects under a running receive_response
                async with contextlib.aclosing(self.client.receive_response()) as messages:
                    found = await _consume(claude, messages, callback, log, progress, started=started,
                                           kind='session', connect_extra=extra)
                self.fresh = False
                return found
            finally:
                self.slot.detach()
                self.answering = None
                self.last_used = time.monotonic()

    async def close(self) -> None:
        """Disconnects, after any message still answering has stopped (cancelled) and closed its messages."""
        answering = self.answering
        if answering is not None and answering is not asyncio.current_task() and not answering.done():
            answering.cancel()
        if self.lock.locked() and answering is not asyncio.current_task():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(CLOSE_WAIT_SECONDS):
                    await self.lock.acquire()
                    self.lock.release()
        if self.connected:
            self.connected = False
            with contextlib.suppress(Exception):
                await self.client.disconnect()


class Lease:
    """One run's hold on a conversation's warm session (SessionPool.conversation)."""

    def __init__(self, pool: 'SessionPool', key: str):
        self.pool, self.key = pool, key

    @property
    def taint(self) -> list[tuple[str, str]]:
        """(source, text) of third-party content earlier messages read: this run starts tainted by it."""
        return list(self.pool.taint.get(self.key, []))

    async def session(self, instructions: str, guarded, model: str) -> WarmSession:
        """The conversation's session, or a new one when there is none or its tools, prompt or model differ."""
        current = self.pool.sessions.get(self.key)
        if current is not None and not current.fits(instructions, guarded, model):
            await self.pool.drop(self.key)
            current = None
        if current is None:
            current = self.pool.sessions[self.key] = WarmSession(instructions, guarded, model)
            self.pool.start_reaper()
        return current

    async def warm(self, instructions: str, guarded, model: str) -> bool:
        """Starts the conversation's session before its first message. False if it couldn't: the session is
        dropped, and the first message starts one as it always did (and shows any problem then)."""
        try:
            await (await self.session(instructions, guarded, model)).start()
            return True
        except Exception as e:
            logger.info(f'Claude Code could not start early: {type(e).__name__}: {e}')
            await self.pool.drop(self.key)
            return False
        except BaseException:  # cancelled (the chat ended before its first message): closed now, not left half open
            await self.pool.drop(self.key)
            raise

    async def ask(self, session: WarmSession, text: str, guarded, *, untrusted_texts=lambda: (), **options):
        """session.ask(); any failure closes and drops the session, so the next message starts fresh. What the run
        read from third parties is kept for the conversation's next messages either way."""
        try:
            return await session.ask(text, guarded, **options)
        except BaseException:
            await self.pool.drop(self.key)
            raise
        finally:
            kept = self.pool.taint.setdefault(self.key, [])
            kept.extend(pair for pair in untrusted_texts() if pair not in kept)


class SessionPool:
    """The warm sessions, by conversation id. Owned by the Runtime; sessions unused for idle_minutes are closed."""

    def __init__(self, idle_minutes: int = 10):
        self.idle_seconds = idle_minutes * 60
        self.sessions: dict[str, WarmSession] = {}
        self.taint: dict[str, list[tuple[str, str]]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None
        self._closing = False

    @contextlib.asynccontextmanager
    async def conversation(self, key: str):
        """Holds the conversation for one run, from before its gate is built (taint) until its answer."""
        async with self.locks.setdefault(key, asyncio.Lock()):
            yield Lease(self, key)

    async def drop(self, key: str) -> None:
        session = self.sessions.pop(key, None)
        if session is not None:
            await session.close()

    async def reap(self, now: float | None = None) -> list[str]:
        """Closes and drops sessions idle longer than idle_seconds (not one that is answering). Their keys."""
        now = time.monotonic() if now is None else now
        idle = [key for key, session in self.sessions.items()
                if now - session.last_used > self.idle_seconds and not session.lock.locked()]
        for key in idle:
            await self.drop(key)
        return idle

    def start_reaper(self) -> None:
        """Closes idle sessions in the background while any are open. Needs no daemon support."""
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.get_running_loop().create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        """Runs while sessions are open. When wraithd shuts down, asyncio.run() cancels it before the event loop
        closes: then it closes every session, in the loop their Claude Code processes belong to."""
        try:
            while self.sessions:
                await asyncio.sleep(min(REAP_EVERY_SECONDS, self.idle_seconds))
                try:
                    await self.reap()
                except Exception as e:
                    logger.warning(f'Could not close idle Claude Code sessions: {type(e).__name__}: {e}')
        finally:
            if self.sessions and not self._closing:
                await asyncio.shield(self._close_sessions())

    async def _close_sessions(self) -> None:
        for key in list(self.sessions):
            await self.drop(key)

    async def close(self) -> None:
        """Closes every session (wraithd is shutting down)."""
        self._closing = True
        try:
            await self._close_sessions()
        finally:
            if self._reaper is not None and self._reaper is not asyncio.current_task():
                self._reaper.cancel()
            self._closing = False

    def close_soon(self) -> None:
        """close() from synchronous code (Runtime.close). Within a running event loop it is scheduled; with none,
        the sessions already closed when their loop ended (the reaper), so only stragglers are dropped."""
        if not self.sessions:
            return
        try:
            asyncio.get_running_loop().create_task(self.close())
        except RuntimeError:  # no loop: their loop is gone, and the Claude Code processes end with wraithd
            self.sessions.clear()
