"""
The Claude Code engine: Anthropic's unmodified Claude Code program runs the thinking loop, signed in with the owner's
own Claude Pro or Max login (claude_options.py has the rules). Claude Code sees only wraithd's guarded tools
(claude_bridge.py), so every action still goes through the gate.

Two ways to run:
- run_once(): one Claude Code process for one run. Background work (heartbeat, schedules), rt.complete() (memory
  distill, compaction) and any run without a conversation.
- WarmSession: one Claude Code process per conversation and model, kept between messages so only the first one
  pays the ~4.6s start. Claude Code fixes its model when it connects, so a conversation with [engine] quick_model on
  keeps up to two: chat_model's and quick_model's (run.py picks one per message). SessionPool (owned by the
  Runtime) keys them by (conversation id, model), shares one taint per conversation between them, and closes a
  conversation's sessions together (idle, /new, /quit, shutdown). `wraithd chat` starts its conversation's sessions
  as soon as it opens (run.warm_up), so the start overlaps the owner's typing; a channel without that (Telegram)
  starts the other model's session right after the first answer (SessionPool.prestart).

wraithd stays the source of truth: its (compacted) history goes to a new session as a transcript, and its event
log records every tool call and the tokens used, so the budgets count.
"""

import asyncio
import contextlib
import gc
import logging
import time
from collections.abc import Mapping

from wraithd.core.loop import MAX_AGENT_STEPS
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


MAX_TURNS = 'error_max_turns'  # the result's subtype when the run used [engine] max_turns model calls


def hit_max_turns(result) -> bool:
    """The run ended because it used all its turns: a stop (run.py answers plainly), not an engine failure."""
    return result is not None and getattr(result, 'subtype', None) == MAX_TURNS


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


def one_shot(claude, prompt, options):
    """
    A one-shot's messages, to close with contextlib.aclosing. The SDK's public query() only wraps its client's
    process_query() and leaves that inner generator for the event loop to close later, so the Claude Code process
    could still be shutting down when asyncio.run() closes the loop ("RuntimeError: Event loop is closed" from
    BaseSubprocessTransport.__del__ as `wraithd doctor` exits). Iterating process_query() itself closes the process,
    in order, before aclosing returns. Any other SDK (or the tests' fake) gets query() as it is.
    """
    client = getattr(claude.query, '__globals__', {}).get('InternalClient')
    if client is None:
        return claude.query(prompt=prompt, options=options)
    return client().process_query(prompt=prompt, options=options)


def _claude_transport(found) -> bool:
    """An asyncio subprocess transport of a Claude Code program (started with --input-format stream-json)."""
    from asyncio.base_subprocess import BaseSubprocessTransport

    if not isinstance(found, BaseSubprocessTransport):  # first: other objects' attributes can do anything
        return False
    args = getattr(getattr(found, '_proc', None), 'args', None) or ()
    return '--input-format' in args and 'stream-json' in args


async def close_leftover_pipes() -> int:
    """
    Closes the pipes of Claude Code programs that have exited but whose stdout something they started still holds
    open: left alone, their transport outlives the event loop, and its __del__ then fails with "Event loop is
    closed". Only exited Claude Code processes: the SDK has stopped reading them, so nothing is lost. How many.
    """
    leftover = [found for found in gc.get_objects()
                if _claude_transport(found) and not found.is_closing() and found.get_returncode() is not None]
    for transport in leftover:
        transport.close()
    if leftover:
        await asyncio.sleep(0)  # the pipes' connection_lost callbacks run now, in this loop
    return len(leftover)


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
                if message.subtype == MAX_TURNS:  # a stop, like the API engine's step limit (core/loop.py)
                    if log is not None:
                        log.append(ev.STOP, reason='max_turns', message=f'Reached {message.num_turns} model calls.')
                    return '', message
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
                   stop=None, log=None, progress: dict | None = None, max_turns: int = MAX_AGENT_STEPS):
    """One run in its own Claude Code process: (answer, ResultMessage or None). guarded: the run's tools after
    guard_function_tools() ([] for a tool-less call); model: [engine] model_for() this purpose; max_turns: [engine]
    max_turns. A run that used all its turns returns ('', its ResultMessage): hit_max_turns() says so."""
    _ready()
    claude = sdk()
    slot = RunSlot()
    server, names = bridge(guarded, slot) if guarded else (None, [])
    slot.attach(guarded, limit, stop)
    options = claude_options(instructions, server, names, model, stream=callback is not None, max_turns=max_turns)
    try:
        # Closed here, in order, before returning: left open, the event loop closes the SDK's nested generators
        # all at once when it ends, and one of them is still closing ("aclose(): ... already running").
        async with contextlib.aclosing(one_shot(claude, prompt, options)) as messages:
            return await _consume(claude, messages, callback, log, progress, started=time.monotonic())
    finally:
        slot.detach()
        await close_leftover_pipes()


async def complete(prompt: str, model: str) -> str:
    """One tool-less call, for the harness's own work (memory distill, compaction, summaries)."""
    answer, _ = await run_once(HELPER_PROMPT, prompt, [], model)
    return answer


class WarmSession:
    """One Claude Code process per conversation, reused between messages. Its tools, system prompt and model are
    fixed when it connects: a run that needs others gets a new session (SessionPool)."""

    def __init__(self, instructions: str, guarded, model: str, max_turns: int = MAX_AGENT_STEPS):
        claude = sdk()
        self.slot = RunSlot()
        server, self.names = bridge(guarded, self.slot)
        self.key, self.model = (tuple(self.names), instructions, model), model
        self.client = claude.ClaudeSDKClient(options=claude_options(instructions, server, self.names, model,
                                                                    stream=True, max_turns=max_turns))
        self.lock = asyncio.Lock()
        self.connected, self.fresh, self.last_used = False, True, time.monotonic()
        self.closed = False  # close() ran: a start() that hadn't begun yet never connects
        self.answering: asyncio.Task | None = None  # the task in ask() or start(), while it runs
        self.early_seconds: float | None = None  # how long start()'s connect took, until the first message logs it

    async def start(self) -> None:
        """Connects now, before any message (`wraithd chat` as it opens), so Claude Code starts while the owner
        types. A message that comes first waits for it (the lock). Its time is logged by the first message."""
        async with self.lock:
            if self.connected or self.closed:
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
        self.closed = True
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
    """One run's hold on a conversation's warm sessions (SessionPool.conversation). key: the conversation id."""

    def __init__(self, pool: 'SessionPool', key: str):
        self.pool, self.key = pool, key

    @property
    def taint(self) -> list[tuple[str, str]]:
        """(source, text) of third-party content earlier messages read: this run starts tainted by it."""
        return list(self.pool.taint.get(self.key, []))

    async def session(self, instructions: str, guarded, model: str, max_turns: int = MAX_AGENT_STEPS) -> WarmSession:
        """The conversation's session on this model, or a new one when there is none or its tools or prompt differ."""
        return await self.pool.session(self.key, instructions, guarded, model, max_turns)

    async def warm(self, instructions: str, guarded, model: str, max_turns: int = MAX_AGENT_STEPS) -> bool:
        """Starts the conversation's session before its first message. False if it couldn't: the session is
        dropped, and the first message starts one as it always did (and shows any problem then)."""
        try:
            await (await self.session(instructions, guarded, model, max_turns)).start()
            return True
        except Exception as e:
            logger.info(f'Claude Code could not start early: {type(e).__name__}: {e}')
            await self.pool.drop(self.key, model)
            return False
        except BaseException:  # cancelled (the chat ended before its first message): closed now, not left half open
            await self.pool.drop(self.key, model)
            raise

    async def ask(self, session: WarmSession, text: str, guarded, *, untrusted_texts=lambda: (), **options):
        """session.ask(); any failure closes and drops the session, so the next message starts fresh. What the run
        read from third parties is kept for the conversation's next messages either way, on either model."""
        try:
            return await session.ask(text, guarded, **options)
        except BaseException:
            await self.pool.drop(self.key, session.model, only=session)
            raise
        finally:
            kept = self.pool.taint.setdefault(self.key, [])
            kept.extend(pair for pair in untrusted_texts() if pair not in kept)


class SessionPool:
    """The warm sessions, by (conversation id, model). Owned by the Runtime; a conversation whose sessions were all
    unused for idle_minutes has them closed. Taint is per conversation: what one model's session read taints the
    other's next run too."""

    def __init__(self, idle_minutes: int = 10):
        self.idle_seconds = idle_minutes * 60
        self.sessions: dict[tuple[str, str], WarmSession] = {}
        self.taint: dict[str, list[tuple[str, str]]] = {}
        self._starting: set[asyncio.Task] = set()  # prestart()s under way
        self.locks: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None
        self._closing = False

    @contextlib.asynccontextmanager
    async def conversation(self, key: str):
        """Holds the conversation for one run, from before its gate is built (taint) until its answer."""
        async with self.locks.setdefault(key, asyncio.Lock()):
            yield Lease(self, key)

    async def session(self, conversation: str, instructions: str, guarded, model: str,
                      max_turns: int = MAX_AGENT_STEPS) -> WarmSession:
        """The conversation's session on this model, or a new one when there is none or its tools or prompt differ
        (only that model's session is replaced)."""
        current = self.sessions.get((conversation, model))
        if current is not None and not current.fits(instructions, guarded, model):
            await self.drop(conversation, model)
            current = None
        if current is None:
            current = self.sessions[(conversation, model)] = WarmSession(instructions, guarded, model, max_turns)
            self.start_reaper()
        return current

    def models_of(self, conversation: str) -> list[str]:
        """The models this conversation has a session on."""
        return [model for key, model in self.sessions if key == conversation]

    def prestart(self, conversation: str, instructions: str, guarded, model: str,
                 max_turns: int = MAX_AGENT_STEPS) -> asyncio.Task | None:
        """
        Starts the conversation's session on this model in the background, if it has none: the other model's, after
        a message (Telegram has no chat-opens moment to start it). It doesn't hold the conversation: a message that
        needs it waits for its start (the session's lock); one on the other model doesn't wait at all. A failure
        only drops it, and the message that needs it starts one as it always did. None when there was nothing to do.
        """
        if (conversation, model) in self.sessions or self._closing:
            return None

        async def start() -> None:
            session = None
            try:
                session = await self.session(conversation, instructions, guarded, model, max_turns)
                await session.start()
            except Exception as e:
                logger.info(f'Claude Code ({model}) could not start early: {type(e).__name__}: {e}')
                if session is not None:
                    await self.drop(conversation, model, only=session)
            except BaseException:  # cancelled (the conversation closed while it started): closed now
                if session is not None:
                    await self.drop(conversation, model, only=session)
                raise

        task = asyncio.get_running_loop().create_task(start())
        self._starting.add(task)
        task.add_done_callback(self._starting.discard)
        return task

    async def drop(self, conversation: str, model: str | None = None, only: WarmSession | None = None) -> None:
        """Closes and drops the conversation's sessions: every model's, or one model's (and then only if it is
        still `only`, when that is given)."""
        keys = [key for key in self.sessions if key[0] == conversation and model in (None, key[1])]
        for key in keys:
            if only is not None and self.sessions.get(key) is not only:
                continue
            session = self.sessions.pop(key, None)
            if session is not None:
                await session.close()

    async def reap(self, now: float | None = None) -> list[str]:
        """Closes and drops the sessions of conversations idle longer than idle_seconds: all of a conversation's
        sessions together, when none was used since and none is answering. The conversations' ids."""
        now = time.monotonic() if now is None else now
        conversations: dict[str, list[WarmSession]] = {}
        for (conversation, _), session in self.sessions.items():
            conversations.setdefault(conversation, []).append(session)
        idle = [conversation for conversation, sessions in conversations.items()
                if all(now - session.last_used > self.idle_seconds and not session.lock.locked()
                       for session in sessions)]
        for conversation in idle:
            await self.drop(conversation)
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
        for task in list(self._starting):
            if task is not asyncio.current_task():
                task.cancel()
        for conversation in {conversation for conversation, _ in self.sessions}:
            await self.drop(conversation)

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
