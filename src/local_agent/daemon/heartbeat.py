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
  time and what the heartbeat already said lately, so the stable prompt prefix doesn't change.
- An answer of HEARTBEAT_OK (or a run that failed) sends nothing: a heartbeat_silent event. Anything else goes to the
  owner through the notifier.
"""

import logging
import re
from contextlib import AsyncExitStack
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from local_agent.config import ConfigError
from local_agent.core import events as ev
from local_agent.core import views
from local_agent.core.gate import ToolAccess
from local_agent.mcp.client import McpConnections
from local_agent.schedules import local_timezone

logger = logging.getLogger(__name__)

SOURCE = 'heartbeat'
TICK_SECONDS = 60  # the job checks this often whether a heartbeat is due
SILENT = 'HEARTBEAT_OK'
MAX_TOOL_CALLS = 10
RECENT_SENT = 3  # earlier heartbeat messages shown, so it doesn't say the same thing twice
SENT_CHARS = 500
CHECKLIST_CHARS = 4_000
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

    def message(self, now: datetime) -> str:
        path = self.rt.home.workspace / 'HEARTBEAT.md'
        try:
            checklist = path.read_text().strip()[:CHECKLIST_CHARS] or DEFAULT_CHECKLIST
        except FileNotFoundError:
            checklist = DEFAULT_CHECKLIST
        sent = [event.payload.get('message', '') for event in self._runs(20) if event.payload.get('sent')]
        recent = '\n'.join(f'- {text}' for text in sent[:RECENT_SENT]) or '(nothing)'
        local = now.astimezone(ZoneInfo(local_timezone(self.rt.config.timezone)))
        return PROMPT.format(now=f'{local:%A %d %B %Y, %H:%M} ({local.tzname()})', silent=SILENT, recent=recent,
                             checklist=checklist)

    async def tick(self, now: datetime | None = None) -> str | None:
        """Runs a heartbeat if one is due. Returns its run id, or None when none was due."""
        now = now or self.clock()
        if not self.settings['enabled'] or not self.due(now):
            return None
        return await self.run(now)

    async def run(self, now: datetime | None = None, mcp=None) -> str:
        """One heartbeat now, due or not. mcp: connections already open; else the configured servers are opened."""
        from local_agent.run import run  # run.py loads the daemon's callers; import at call time

        now = now or self.clock()
        async with AsyncExitStack() as stack:
            if mcp is None and self.rt.config.mcp_servers:
                mcp = await stack.enter_async_context(self.mcp_factory(self.rt.config.mcp_servers, store=self.rt.store))
            result = await run(self.rt, self.message(now), source=SOURCE, callback=None, mcp=mcp, purpose='heartbeat',
                               budget={'run_tokens': self.settings['max_tokens'], 'run_tool_calls': MAX_TOOL_CALLS},
                               allowed_tools=allowed_tools(mcp))
        tokens = views.tokens_used(self.rt.store.run_events(result.run_id))
        answer = result.answer.strip()
        sent = result.outcome == 'answered' and bool(answer) and not answer.upper().startswith(SILENT)
        if sent:
            await self.rt.notifier.deliver(answer)
        ev.record_event(self.rt.store, ev.HEARTBEAT_RUN, run_id=result.run_id, at=now.isoformat(),
                        outcome=result.outcome, tokens=tokens, sent=sent,
                        **({'message': answer[:SENT_CHARS]} if sent else {}))
        if not sent:
            ev.record_event(self.rt.store, ev.HEARTBEAT_SILENT, run_id=result.run_id, outcome=result.outcome)
        return result.run_id


async def run_once(rt, mcp=None, now: datetime | None = None) -> str:
    """One heartbeat now, whatever the interval (evals, `local-agent heartbeat now`). Returns its run id."""
    return await Heartbeat(rt).run(now, mcp)


async def heartbeat_job(rt) -> str | None:
    """The daemon's periodic job (daemon/jobs.py): checks every minute whether a heartbeat is due."""
    return await Heartbeat(rt).tick()


# --- `local-agent heartbeat ...` --------------------------------------------------------------------------------

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

    from local_agent.run import Runtime

    rt = Runtime.open(home)
    try:
        if args.heartbeat_command == 'status':
            print(status_text(rt))
            return 0
        run_id = asyncio.run(run_once(rt))
        print(f'Heartbeat run {run_id[:8]}: see `local-agent runs show {run_id[:8]}`')
        return 0
    finally:
        rt.close()
