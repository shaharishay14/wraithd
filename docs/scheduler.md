# The scheduler

**Files:** `daemon/scheduler.py`, `schedules.py`

## One-liner

> Inside the daemon, every minute: `schedules.due(store, now)` claims what's due, each claimed schedule runs as an
> unattended run, and its answer goes to the owner.

## How it works

- **When:** the loop wakes at the start of each minute, so "8:00" fires at 8:00:0x.
- **Claiming:** `due()` moves each due schedule to its next time (a one-off is turned off) with a compare-and-set
  before it runs ([schedules](schedules.md)). So a slot fires at most once, and a run that crashes is lost, not
  repeated: a repeat could send the same email twice.
- **The run:** `run(message, source='schedule', callback=None)`, with the daemon's open MCP connections. No
  callback means nobody can approve: the shell isn't offered, the gate refuses it outright, and writes run only if
  the owner always-allowed them (and then only to recipients the owner wrote, once third-party content is in).
- **The answer:** delivered with `⏰ <the schedule's message>` on top, through the notifier: the owner's Telegram
  chat when one is paired, otherwise the "Notifications" conversation in the web UI. The agent can also use
  `notify_owner` during the run.
- **Silent runs:** an answer that is exactly `NO_REPLY` (after trimming spaces) delivers nothing, like the
  heartbeat's `HEARTBEAT_OK`. A skill that sends its items with `notify_owner` ends a scheduled run that way, so no
  extra "⏰" message follows them. The run and its answer are still in the run log (`wraithd runs show`). The same
  rule holds on every channel (core/silent.py: Telegram, the terminal chat and the web chat show nothing for it).
- **Tests** call `tick(now)` with a fake clock: nothing fires before its time, it fires once at it, and a terminal
  session's schedule fires in a daemon with no terminal, answering in the owner's (fake) Telegram chat. A
  `NO_REPLY` answer sends nothing and any other answer still does (`tests/daemon/test_scheduler.py`).

## On a laptop

agent-studio's dispatcher was Celery Beat on a server. Here the scheduler only runs while the daemon does: a
schedule due while the laptop is off or asleep fires when the daemon next ticks, once. The background service
([installing and running](install.md)) starts the daemon at login.
