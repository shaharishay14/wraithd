# The heartbeat

**Files:** `daemon/heartbeat.py`, the capabilities block in `daemon/jobs.py`

## One-liner

> Every half hour the agent looks around on its own (reminders, mail, the owner's checklist) and messages the owner
> only when something needs them. Most heartbeats end silently. It only ever reads.

## How it works

- **Off by default.** `[heartbeat]`: `enabled = false`, `every_minutes = 30`, `quiet_hours = "22:00-07:00"` (local
  time, may wrap past midnight; `""` for none), `max_tokens = 20000`.
- **When:** the daemon's `heartbeat` job checks every minute. A heartbeat is due when the last `heartbeat_run` is
  `every_minutes` old and it isn't quiet hours, so a restart doesn't make it fire early.
- **The run:** unattended (no approval callback), `source` and `purpose` `heartbeat` (`rt.model_for('heartbeat')`),
  capped at `max_tokens` and 10 tool calls.
- **Read-only, by list.** `allowed_tools` is an explicit list: `memory_search`, `memory_read`, `files_list`,
  `files_read`, `schedule_list`, `web_search`, `read_website`, `skill_load`, the discovery tools, and the apps' tools
  their server marks read-only. Not the gate's READ tier: `memory_append`, `memory_write`, `files_write`,
  `files_edit`, `skill_propose` and `schedule_cancel` are READ there (they need no approval) but change things. So the
  heartbeat never writes, not even a pre-approved write. A tool that isn't offered can't run.
- **What it looks at:** `workspace/HEARTBEAT.md`, the owner's checklist (a default one without it), the time, and
  the last three things it told the owner, all in the run's message. The system prompt stays the stable prefix.
- **Silent or not:** an answer of `HEARTBEAT_OK`, or a run that failed, sends nothing: `heartbeat_run` with
  `sent: false`, plus `heartbeat_silent`. Anything else goes to the owner through the notifier (Telegram, or the
  inbox), and `heartbeat_run` keeps the message (500 characters) so the next one doesn't repeat it.
- **Commands:** `local-agent heartbeat status`, `local-agent heartbeat now` (one now, whatever the interval). For
  evals: `await heartbeat.run_once(rt, mcp=None, now=None)`.

## On a laptop

A laptop sleeps: a heartbeat due while it's asleep runs at the next tick after it wakes, once. Quiet hours keep it from
buzzing the owner's phone at night.
