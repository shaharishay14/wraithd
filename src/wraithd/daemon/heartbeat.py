"""
The heartbeat: every so often the agent looks around on its own and decides whether anything is worth telling the
owner. Most heartbeats end silently. Off by default ([heartbeat] enabled = true turns it on).

- When: every `every_minutes`, never inside `quiet_hours` (local time, "22:00-07:00"; "" for none). The last time
  is read from the heartbeat_run events, so a restart doesn't make it fire early.
- What: one unattended run (source and purpose 'heartbeat', no approval callback) with read-only tools only, an
  explicit list rather than the gate's READ tier, since some READ-tier built-ins write (memory_append, files_write,
  skill_propose, schedule_cancel...). Connected apps: only the tools their server marks read-only. The heartbeat
  never writes, not even a pre-approved write. Its run is capped at `max_tokens`.
- The owner's checklist is workspace/HEARTBEAT.md (a default one without it). It goes in the run's message, with the
  time and what the owner was told lately, so the stable prompt prefix doesn't change. "Told lately" is the newest
  messages in the Notifications inbox (heartbeats, schedules, notify_owner, review notices) merged with the
  heartbeat_run messages, which also cover heartbeats Telegram delivered (those skip the inbox).
- An answer of HEARTBEAT_OK (or a run that failed) sends nothing: a heartbeat_silent event. Anything else goes to the
  owner through the notifier.
- Two modes, recorded on heartbeat_run and heartbeat_silent as `mode`. Lean (the usual one): the code looks up what
  the model would (schedule_list's answer, and memory_search for "remind me" and "reminder", as those tools return
  them) and puts it in the message; the run gets a short prompt and no tools, so it is one model turn. Full: today's
  run with the read-only tools. Full whenever lean could miss something: a connected app offers read-only tools
  (email, calendar...: the code doesn't fetch those), or the owner wrote a workspace/HEARTBEAT.md (their checklist
  may ask for anything).
"""

import json
import logging
import re
from contextlib import AsyncExitStack
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from wraithd.config import ConfigError
from wraithd.core import events as ev
from wraithd.core import views
from wraithd.core.gate import ToolAccess
from wraithd.mcp.client import McpConnections
from wraithd.memory import tools as memory_tools
from wraithd.memory import vault
from wraithd.schedules import local_timezone
from wraithd.tools import schedules as schedule_tools
from wraithd.tools.notify import INBOX

logger = logging.getLogger(__name__)

SOURCE = 'heartbeat'
TICK_SECONDS = 60  # the job checks this often whether a heartbeat is due
SILENT = 'HEARTBEAT_OK'
MAX_TOOL_CALLS = 10
RECENT_SENT = 3  # earlier messages to the owner shown, so it doesn't say the same thing twice
SENT_CHARS = 500
CHECKLIST_CHARS = 4_000
LEAN, FULL = 'lean', 'full'
REMINDER_QUERIES = ('remind me', 'reminder')  # what a full heartbeat searches memory for
QUIET = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$')

# Built-in tools that only read. Not the gate's READ tier: memory_append/write, files_write/edit, skill_propose and
# schedule_cancel are READ there (they need no approval) but change things. The discovery tools stay when apps are
# connected, and search only the read-only actions.
READ_ONLY_BUILTINS = frozenset({
    'memory_search', 'memory_read', 'files_list', 'files_read', 'schedule_list', 'web_search', 'read_website',
    'skill_load', 'find_app_actions', 'describe_app_action',
})

DEFAULT_CHECKLIST = """- Reminders and schedules coming up today (schedule_list)
- New email or calendar changes that need the owner soon, if those apps are connected
- Anything in memory the owner asked to be reminded about"""

PROMPT = """Heartbeat check, {now}. You are running on your own: nobody is watching, and you can only read.
Go through the checklist below. If nothing needs the owner's attention now, answer exactly {silent} and nothing \
else. Otherwise answer with the short message to send them (it is sent as written). Don't repeat what you already \
told them.

Already told them lately:
{recent}

Checklist (the owner's):
{checklist}"""

# Lean mode: the system prompt (the standing orders and the memory profile follow it), then the message
LEAN_PROMPT = ('You are wraithd, a personal assistant that runs on the user\'s own computer. This is a heartbeat '
               'check: you have no tools, and what you need was looked up for you in the message. Never claim you '
               'did something.\n')

LEAN_CHECKLIST = """- Reminders and schedules coming up today
- Anything in memory the owner asked to be reminded about"""

LEAN_MESSAGE = """Heartbeat check, {now}. You are running on your own: nobody is watching, and you have no tools: \
what you'd look up is below. If nothing needs the owner's attention now, answer exactly {silent} and nothing else. \
Otherwise answer with the short message to send them (it is sent as written). Don't repeat what you already told them.

Already told them lately:
{recent}

Checklist:
{checklist}

Schedules and reminders ({schedule_list}):
{schedules}

Memory notes about reminders ({memory_search} {queries}):
{notes}"""


def check(values: dict) -> None:
    """[heartbeat] settings: config.py's Section check."""
    if values['every_minutes'] < 5:
        raise ConfigError('[heartbeat] every_minutes must be at least 5.')
    if values['max_tokens'] < 1_000:
        raise ConfigError('[heartbeat] max_tokens must be at least 1000.')
    if values['quiet_hours'] and not QUIET.match(values['quiet_hours']):
        raise ConfigError('[heartbeat] quiet_hours must look like "22:00-07:00" (or "" for none).')


def in_quiet_hours(quiet_hours: str, local: datetime) -> bool:
    """True if local time is inside "HH:MM-HH:MM" (which may wrap past midnight)."""
    match = QUIET.match(quiet_hours or '')
    if not match:
        return False
    start, end = time(int(match[1]), int(match[2])), time(int(match[3]), int(match[4]))
    now = local.time().replace(second=0, microsecond=0)
    if start == end:
        return False
    return start <= now < end if start < end else (now >= start or now < end)


def allowed_tools(mcp) -> set[str]:
    """What a heartbeat run may be offered: the read-only built-ins, and the apps' read-only tools."""
    apps = {name for name, access in (mcp.access if mcp else {}).items()
            if access == ToolAccess.READ and name not in mcp.destructive}
    return set(READ_ONLY_BUILTINS) | apps


def app_tools(mcp) -> set[str]:
    """The connected apps' tools a heartbeat may use (email, calendar...): lean mode doesn't fetch these."""
    return allowed_tools(mcp) - READ_ONLY_BUILTINS


class Heartbeat:
    def __init__(self, rt, clock=lambda: datetime.now(UTC), mcp_factory=McpConnections):
        self.rt, self.clock, self.mcp_factory = rt, clock, mcp_factory

    @property
    def settings(self) -> dict:
        return self.rt.config.section('heartbeat')

    def _runs(self, limit: int):
        return self.rt.store.events_between_runs(limit, (ev.HEARTBEAT_RUN,))

    def last_run_at(self) -> datetime | None:
        for event in self._runs(1):
            try:
                return datetime.fromisoformat(event.payload['at'])
            except (KeyError, TypeError, ValueError):
                return event.created_at
        return None

    def due(self, now: datetime) -> bool:
        settings = self.settings
        local = now.astimezone(ZoneInfo(local_timezone(self.rt.config.timezone)))
        if in_quiet_hours(settings['quiet_hours'], local):
            return False
        last = self.last_run_at()
        return last is None or now - last >= timedelta(minutes=settings['every_minutes'])

    def recently_told(self) -> list[str]:
        """The last RECENT_SENT messages to the owner, newest first, each on one line and cut to SENT_CHARS: the inbox
        (Notifications) and the heartbeat_run messages, merged, a message in both counted once."""
        store = self.rt.store
        told = [(event.created_at, event.payload.get('message', '')) for event in self._runs(20)
                if event.payload.get('sent')]
        inbox = store.find_conversation(*INBOX)
        if inbox is not None:
            told += [(message.created_at, message.content) for message in store.messages(inbox.id)[-RECENT_SENT * 4:]
                     if message.role == 'assistant']
        shown: list[str] = []
        for _, text in sorted(told, key=lambda item: item[0], reverse=True):
            text = ' '.join(text.split())[:SENT_CHARS]  # one line each: a schedule's answer has blank lines
            if text and text not in shown:
                shown.append(text)
        return shown[:RECENT_SENT]

    def checklist(self) -> str:
        """The owner's workspace/HEARTBEAT.md, or '' when there's none (or it's empty)."""
        try:
            return (self.rt.home.workspace / 'HEARTBEAT.md').read_text().strip()[:CHECKLIST_CHARS]
        except FileNotFoundError:
            return ''

    def mode(self, mcp) -> tuple[str, str]:
        """(LEAN or FULL, why). Full whenever lean could miss what a full heartbeat would see."""
        if app_tools(mcp):
            return FULL, 'apps'
        if self.checklist():
            return FULL, 'checklist'
        return LEAN, ''

    def _now_text(self, now: datetime) -> str:
        local = now.astimezone(ZoneInfo(local_timezone(self.rt.config.timezone)))
        return f'{local:%A %d %B %Y, %H:%M} ({local.tzname()})'

    def _recent_text(self) -> str:
        return '\n'.join(f'- {text}' for text in self.recently_told()) or '(nothing)'

    def message(self, now: datetime) -> str:
        """The full heartbeat's message."""
        return PROMPT.format(now=self._now_text(now), silent=SILENT, recent=self._recent_text(),
                             checklist=self.checklist() or DEFAULT_CHECKLIST)

    async def lookups(self) -> tuple[str, str, bool]:
        """
        What a full heartbeat looks up, fetched here: (schedule_list's answer, the memory_search results for
        REMINDER_QUERIES merged, whether a low-trust note is among them), each as the tool returns it.
        """
        [listing] = [tool for tool in schedule_tools.schedule_tools(self.rt.store, local_timezone(
            self.rt.config.timezone)) if tool.name == schedule_tools.LIST]
        schedules = await listing.on_invoke_tool(None, '{}')
        notes = self.rt.memory.load()
        results = []
        for query in REMINDER_QUERIES:
            results += [result for result in vault.search(notes, query)
                        if result['path'] not in {seen['path'] for seen in results}]
        untrusted = any(result['trust'] == vault.LOW for result in results)
        found = json.dumps({'results': results, 'untrusted': untrusted} if results else {'results': []},
                           ensure_ascii=False)
        return schedules, found, untrusted

    async def lean_message(self, now: datetime) -> tuple[str, bool]:
        """The lean heartbeat's message, with the lookups in it, and whether it holds third-party text."""
        schedules, notes, untrusted = await self.lookups()
        return LEAN_MESSAGE.format(
            now=self._now_text(now), silent=SILENT, recent=self._recent_text(), checklist=LEAN_CHECKLIST,
            schedule_list=schedule_tools.LIST, schedules=schedules, memory_search=memory_tools.SEARCH,
            queries=', '.join(f'"{query}"' for query in REMINDER_QUERIES), notes=notes), untrusted

    async def tick(self, now: datetime | None = None) -> str | None:
        """Runs a heartbeat if one is due. Returns its run id, or None when none was due."""
        now = now or self.clock()
        if not self.settings['enabled'] or not self.due(now):
            return None
        return await self.run(now)

    async def run(self, now: datetime | None = None, mcp=None) -> str:
        """One heartbeat now, due or not. mcp: connections already open; else the configured servers are opened."""
        from wraithd.run import run  # run.py loads the daemon's callers; import at call time

        now = now or self.clock()
        async with AsyncExitStack() as stack:
            if mcp is None and self.rt.config.mcp_servers:
                mcp = await stack.enter_async_context(self.mcp_factory(self.rt.config.mcp_servers, store=self.rt.store))
            mode, why = self.mode(mcp)
            options = {'source': SOURCE, 'callback': None, 'mcp': mcp, 'purpose': 'heartbeat',
                       'budget': {'run_tokens': self.settings['max_tokens'], 'run_tool_calls': MAX_TOOL_CALLS}}
            if mode == LEAN:
                message, untrusted = await self.lean_message(now)
                # One turn: no tools, a short prompt. A low-trust note in the lookups is labelled as such
                result = await run(self.rt, message, builtins=False, allowed_tools=set(), system_prompt=LEAN_PROMPT,
                                   inherited_taint=((memory_tools.SEARCH, message),) if untrusted else (), **options)
            else:
                result = await run(self.rt, self.message(now), allowed_tools=allowed_tools(mcp), **options)
        tokens = views.tokens_used(self.rt.store.run_events(result.run_id))
        answer = result.answer.strip()
        sent = result.outcome == 'answered' and bool(answer) and not answer.upper().startswith(SILENT)
        if sent:
            await self.rt.notifier.deliver(answer)
        ev.record_event(self.rt.store, ev.HEARTBEAT_RUN, run_id=result.run_id, at=now.isoformat(),
                        outcome=result.outcome, tokens=tokens, sent=sent, mode=mode,
                        **({'full_because': why} if why else {}), **({'message': answer[:SENT_CHARS]} if sent else {}))
        if not sent:
            ev.record_event(self.rt.store, ev.HEARTBEAT_SILENT, run_id=result.run_id, outcome=result.outcome,
                            mode=mode)
        return result.run_id


async def run_once(rt, mcp=None, now: datetime | None = None) -> str:
    """One heartbeat now, whatever the interval (evals, `wraithd heartbeat now`). Returns its run id."""
    return await Heartbeat(rt).run(now, mcp)


async def heartbeat_job(rt) -> str | None:
    """The daemon's periodic job (daemon/jobs.py): checks every minute whether a heartbeat is due."""
    return await Heartbeat(rt).tick()


# --- `wraithd heartbeat ...` --------------------------------------------------------------------------------

def add_arguments(subparsers) -> None:
    beat = subparsers.add_parser('heartbeat', help='the periodic look-around (off unless [heartbeat] enabled)'
                                 ).add_subparsers(dest='heartbeat_command', required=True)
    beat.add_parser('status', help='on or off, when it last ran, what it said')
    beat.add_parser('now', help='run one heartbeat now (a model call), and print what it would send')


def status_text(rt, now: datetime | None = None) -> str:
    settings, beat = rt.config.section('heartbeat'), Heartbeat(rt)
    now = now or datetime.now(UTC)
    lines = [f'Heartbeat: {"on" if settings["enabled"] else "off ([heartbeat] enabled = true turns it on)"}, every '
             f'{settings["every_minutes"]} minutes, quiet {settings["quiet_hours"] or "never"}, at most '
             f'{settings["max_tokens"]} tokens']
    last = beat.last_run_at()
    lines.append(f'Last ran: {last.astimezone():%Y-%m-%d %H:%M}' if last else 'Last ran: never')
    if settings['enabled']:
        lines.append(f'Due now: {"yes" if beat.due(now) else "no"}')
    for event in beat._runs(5):
        said = event.payload.get('message') if event.payload.get('sent') else '(nothing)'
        lines.append(f'  {event.created_at.astimezone():%m-%d %H:%M}  {said}')
    return '\n'.join(lines)


def main(home, args) -> int:
    import asyncio

    from wraithd.run import Runtime

    rt = Runtime.open(home)
    try:
        if args.heartbeat_command == 'status':
            print(status_text(rt))
            return 0
        run_id = asyncio.run(run_once(rt))
        print(f'Heartbeat run {run_id[:8]}: see `wraithd runs show {run_id[:8]}`')
        return 0
    finally:
        rt.close()
