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
- **Two modes**, recorded as `mode` on `heartbeat_run` and `heartbeat_silent`:
  - **Lean** (the usual one): one model turn, no tools. The code looks up what a full heartbeat would: the schedules
    (`schedule_list`'s answer, as the tool returns it) and memory notes about reminders (`memory_search` for
    "remind me" and "reminder", merged), and puts them in the message. The system prompt is a short heartbeat-only
    one (`LEAN_PROMPT`, then the standing orders and the memory profile; no skills list, no reminders), via
    `run(..., system_prompt=...)`. A low-trust note among the lookups marks the run's input as third-party
    (`untrusted_inputs: ["memory_search"]`). About 3k tokens instead of ~11.5k over 3-4 turns.
  - **Full**: the run with the read-only tools below, as before. It runs whenever lean could miss something a full
    heartbeat would see: a connected app offers read-only tools (email, calendar...: lean doesn't fetch those;
    `full_because: "apps"`), or the owner wrote a non-empty `workspace/HEARTBEAT.md` (their checklist may ask for
    anything; `full_because: "checklist"`).
- **Read-only, by list** (full mode). `allowed_tools` is an explicit list: `memory_search`, `memory_read`, `files_list`,
  `files_read`, `schedule_list`, `web_search`, `read_website`, `skill_load`, the discovery tools, and the apps' tools
  their server marks read-only. Not the gate's READ tier: `memory_append`, `memory_write`, `files_write`,
  `files_edit`, `skill_propose` and `schedule_cancel` are READ there (they need no approval) but change things. So the
  heartbeat never writes, not even a pre-approved write. A tool that isn't offered can't run.
- **What it looks at:** `workspace/HEARTBEAT.md`, the owner's checklist (a default one without it), the time, and
  the last three things the owner was told ("Already told them lately", newest first, one line each), all in the
  run's message. Those come from the Notifications inbox (heartbeats, schedule answers, `notify_owner`, review
  notices) merged with the `heartbeat_run` messages, which also cover heartbeats Telegram delivered; a message in
  both is listed once. Schedules and `notify_owner` messages Telegram delivered aren't in the inbox, so they aren't
  listed. The system prompt stays the stable prefix.
- **Silent or not:** an answer of `HEARTBEAT_OK`, or a run that failed, sends nothing: `heartbeat_run` with
  `sent: false`, plus `heartbeat_silent`. Anything else goes to the owner through the notifier (Telegram, or the
  inbox), and `heartbeat_run` keeps the message (500 characters) so the next one doesn't repeat it.
- **Commands:** `wraithd heartbeat status`, `wraithd heartbeat now` (one now, whatever the interval). For
  evals: `await heartbeat.run_once(rt, mcp=None, now=None)`.

## On a laptop

A laptop sleeps: a heartbeat due while it's asleep runs at the next tick after it wakes, once. Quiet hours keep it from
buzzing the owner's phone at night.
