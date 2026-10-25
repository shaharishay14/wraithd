"""
Schedules and reminders: "every weekday at 8" is a cron, "remind me Friday at 9" is one ISO time. Both are read in
the owner's timezone (8:00 means 8:00 where they are, across daylight-saving changes) and stored as the next fire
time in UTC.

due(now) is what the daemon (Phase 5) calls every minute: it returns the schedules to run now and moves each one
to its next time first, so a schedule fires at most once per slot, and a laptop that slept through three slots
fires once when it wakes, not three times.
"""

import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from local_agent.store import Schedule

MIN_INTERVAL_SECONDS = 15 * 60  # an agent run every minute would burn the budget for nothing
MAX_MESSAGE_CHARS = 1000


class ScheduleError(ValueError):
    """The schedule can't be created. The message says what to change."""


def local_timezone(configured: str = '') -> str:
    """The owner's IANA timezone: the config's, else $TZ, else this computer's, else UTC."""
    for name in (configured, os.environ.get('TZ', '')):
        if name:
            try:
                ZoneInfo(name)
                return name
            except (ZoneInfoNotFoundError, ValueError):
                pass
    try:
        # /etc/localtime is a symlink into the zoneinfo database on macOS and most Linux systems
        target = os.readlink('/etc/localtime')
        name = target.split('zoneinfo/', 1)[1]
        ZoneInfo(name)
        return name
    except (OSError, IndexError, ZoneInfoNotFoundError, ValueError):
        pass
    try:
        name = Path('/etc/timezone').read_text().strip()  # Debian and Ubuntu
        ZoneInfo(name)
        return name
    except (OSError, ZoneInfoNotFoundError, ValueError):
        return 'UTC'


def next_cron_time(cron: str, timezone: str, after: datetime) -> datetime:
    """The first time strictly after `after` that the cron matches, read in `timezone`, as UTC."""
    return croniter(cron, after.astimezone(ZoneInfo(timezone))).get_next(datetime).astimezone(UTC)


def parse_at(at: str, timezone: str) -> datetime:
    """An ISO time as UTC. Without an offset it's the owner's local time."""
    try:
        moment = datetime.fromisoformat(at.strip())
    except (AttributeError, ValueError):
        raise ScheduleError(f'{at!r} is not an ISO time; use e.g. "2026-11-06T09:00".') from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(timezone))
    return moment.astimezone(UTC)


def create(store, message: str, timezone: str, now: datetime, cron: str | None = None,
           at: str | None = None) -> Schedule:
    """Validates and stores a schedule. Exactly one of cron and at."""
    message = ' '.join((message or '').split())
    if not message:
        raise ScheduleError('A schedule needs the message to run, e.g. "Send me my morning brief".')
    if len(message) > MAX_MESSAGE_CHARS:
        raise ScheduleError(f'Keep the message under {MAX_MESSAGE_CHARS} characters.')
    if bool(cron) == bool(at):
        raise ScheduleError('Give either "cron" (repeating) or "at" (once), not both or neither.')
    if cron:
        cron = ' '.join(cron.split())
        if not croniter.is_valid(cron) or len(cron.split()) != 5:
            raise ScheduleError(f'{cron!r} is not a 5-field cron, e.g. "0 8 * * 1-5" (8:00 on weekdays).')
        first = next_cron_time(cron, timezone, now)
        if (next_cron_time(cron, timezone, first) - first).total_seconds() < MIN_INTERVAL_SECONDS:
            raise ScheduleError(f'Repeating schedules must be at least {MIN_INTERVAL_SECONDS // 60} minutes apart.')
        return store.add_schedule(message, cron, None, timezone, first)
    when = parse_at(at, timezone)
    if when <= now:
        raise ScheduleError(f'{at} is in the past (it is now {now.astimezone(ZoneInfo(timezone)):%Y-%m-%d %H:%M}).')
    return store.add_schedule(message, None, at.strip(), timezone, when)


def due(store, now: datetime) -> list[Schedule]:
    """
    The schedules to run now, each already moved to its next time (a one-off is turned off). A schedule another
    caller claimed first isn't returned, so it runs once even if two processes ask.
    """
    claimed = []
    for schedule in store.due_schedules(now):
        following = next_cron_time(schedule.cron, schedule.timezone, now) if schedule.cron else None
        if store.claim_schedule(schedule.id, schedule.next_fire_at, following, fired_at=now):
            claimed.append(schedule)
    return claimed


def describe(schedule: Schedule) -> str:
    """'every: 0 8 * * 1-5' or 'once: 2026-11-06T09:00', plus the next time in the owner's timezone."""
    when = f'every: {schedule.cron}' if schedule.cron else f'once: {schedule.at}'
    if schedule.next_fire_at and schedule.active:
        local = schedule.next_fire_at.astimezone(ZoneInfo(schedule.timezone))
        when += f' (next {local:%a %Y-%m-%d %H:%M} {schedule.timezone})'
    return when
