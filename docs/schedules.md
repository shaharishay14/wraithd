# Schedules and reminders

**Files:** `schedules.py`, `tools/schedules.py`, `store/`

## One-liner

> "Every weekday at 8" becomes a cron and "remind me Friday at 9" one ISO time, both read in the owner's
> timezone. The owner approves each schedule, because it's a standing instruction that later runs on its own.

## How it works

- **Tools:** `schedule_create(message, cron | at)`, `schedule_list`, `schedule_cancel(id)`. The model turns the
  owner's words into a 5-field cron or an ISO time; the harness validates them (a real cron, at least 15 minutes
  apart, a one-off in the future) and stores the next fire time in UTC.
- **The owner's timezone:** `[owner] timezone` in the config, else `$TZ`, else the computer's own (the
  `/etc/localtime` link), else UTC. A cron is evaluated in that zone, so 8:00 stays 8:00 across daylight saving.
- **Approval:** creating a schedule is a write, shown on the approval card with "your agent will run this on its
  own". Listing and cancelling aren't. Unattended runs can't create schedules (nobody to approve).
- **`due(now)`:** what the daemon (Phase 5) will call every minute. It returns the schedules due now and, before
  returning each, moves it to its next time (a one-off is turned off) with a compare-and-set, so a slot fires at
  most once even if two processes ask, and a laptop that slept through three slots fires once when it wakes.
- **CLI:** `local-agent schedules list [--all]` and `local-agent schedules rm <id>`.

## What's not here yet

Nothing fires a schedule until the daemon exists (Phase 5). The tool tells the model to say so, so it doesn't
promise a reminder that won't come. `due()` is tested on a fake clock, including a daylight-saving change and a
week of missed slots.

## On a laptop

agent-studio ran schedules with Celery Beat on a server that never sleeps. A laptop sleeps, travels across
timezones and gets closed at night. So: the timezone comes from the machine unless the owner pins one, missed
slots fire once rather than piling up, and the claim is a single SQL update instead of a Redis lock.
