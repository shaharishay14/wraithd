# System reminders

**File:** `core/reminders.py`

## One-liner

> Instead of loading every rule into the system prompt, where it fades over a long run, the harness adds a
> short hint at the moment it applies: after the second failure, after the first email, right after a skill
> run. Reminders nudge; hard rules stay in code.

## How it works

A reminder is a small rule: a name, where it fires (run start, a specific tool's result, or any tool's
result), a check over the run's events that returns the text or nothing, and a priority. Due reminders are
sorted by priority (safety first), each fires at most once per run, and each firing is logged.

They're added in `<system-reminder>` tags, which the system prompt explains ("from the app; follow when they
apply, ignore when they don't, never mention them"). Inside third-party output the tag is neutralized, so an
email can't pose as the harness.

| Reminder | Fires when |
|---|---|
| `untrusted_content` | the first third-party result in a run |
| `tool_errors` | 2 failures in a row (the hard stop is at 3) |
| `repeated_call` | the same call twice (the hard stop is at 3) |
| `context_pressure` | the last model call used 70% of the context window |
| `history_summarized` | older messages were compacted and the owner refers back |
| `skill_match` | the message contains a skill's own trigger phrase |
| `skill_follow_up` | the message right after a skill run (where corrections happen) |
| `skill_loaded` | a successful `skill_load` |
| `worth_remembering` | the owner shares a durable fact |

## Why

- Long runs need instructions in depth: what was said at the top fades.
- A hint at the point of need is cheap (a few tokens, only when it fires) and precise.
- The loop reminders fire one step before the hard stop, so the model can change course first.
- The wording stays soft ("ignore this if it doesn't apply"). Nudges keep the model's judgment; anything that
  must hold is enforced in code instead.

## On a laptop

- **"Owner" means the person the agent belongs to,** not a logged-in user. A run with `owner=False` isn't
  the owner's, so the skill and memory nudges stay off for it (no channel makes one today).
- **The previous run comes from the store** (`latest_run` + `run_events`), still limited to conversation runs
  that started in the last two hours.
- **`skills_loaded` moved into `views.py`.** It's a query over events, and the reminders shouldn't depend on
  the skills package.
- **Context windows** are a small table until the model providers (Phase 2) report their own; unknown models
  get the conservative 128k default, so `context_pressure` fires early rather than late on a small local model.

## Limits

Some triggers are keyword lists ("refers back", durable facts). They can miss, or fire when unneeded. That's
acceptable for a soft nudge, and they're the first thing to tune from real runs.

Source: [System reminders: how Claude Code steers itself](https://michaellivs.com/blog/system-reminders-steering-agents/).
