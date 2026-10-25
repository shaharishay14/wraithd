# The event log

**Files:** `core/events.py`, `core/views.py`, `core/stop.py`, `core/hooks.py`, `core/loop.py`, `store/`

## One-liner

> Every step of a run (model calls, tool calls with the gate's decision, results, reminders, stops) is an
> append-only event. The run's state, like "three failures in a row", is a query over those events, not a
> flag someone has to remember to update.

## How it works

- **Events**: `run_start`, `context_built`, `turn_start`, `model_output` (with token usage), `tool_call`
  (with access level and allowed/blocked), `tool_result` (ok/error/denied, untrusted, redacted), `reminder`,
  `stop`, `run_end`. Kept in memory during the run and saved in one batch at the end, so logging never slows
  the loop. Long strings are cut and secrets redacted before anything is stored.
- **Views** are plain functions over the list: `consecutive_tool_errors`, `most_repeated_call`,
  `tokens_used`, `untrusted_content_seen`, `skills_loaded`... They run live before each model call or later
  from the store, and they're trivially unit-testable.
- **Stop conditions** run in the SDK's `on_llm_start` hook, before the call is paid for: 3 tool errors in a
  row, the same call 3 times, 40 tool calls, the run's token budget, the daily budget. A stop raises
  `RunStopped` (an `AgentsException`, so the SDK attaches what the run did so far) and the model gets one last
  call without tools to answer honestly with what it has. The SDK's own turn limit (8 model calls) ends the
  same way.
- **Two renderings of every tool result:** a capped text for the model, structured facts for the log.
- **A run that fails after it streamed text or ran a tool** raises `RunAlreadyStartedError`, so the caller
  never retries it: a retry could send the same email twice.

## Why

- The log is the single source of truth, so the stop rules, the reminders, replay tests and (later) the run
  inspector all read the same thing and can't disagree.
- Deriving state means there's no bookkeeping to forget. A new rule is a new query.
- "Why did it stop?" has an answer: the `stop` event names the rule.

## On a laptop

- **The store replaces the ORM.** agent-studio wrote `RunEvent` rows through Django. Here the harness talks
  to a `Store` protocol with only the queries it makes (save a run's events, read one back, find the previous
  conversation run, add up today's tokens, the approval trail), and `SQLiteStore` implements it on one file
  with the standard library's `sqlite3`. Payloads are JSON text, queried with `json_extract`.
- **No users, no agents.** One process serves one owner, so events have no user or agent columns, and the
  "per-user daily budget" became one daily budget for the machine. It's off by default; the per-run limits
  stay on, because a runaway run costs the owner's own API key.
- **Time is UTC in the store, midnight is local.** Timestamps are stored as fixed-width UTC strings so they
  compare correctly as text; the daily budget resets at the owner's local midnight.
- **Synchronous store calls.** A local SQLite write takes well under a millisecond, so async code calls the
  store directly instead of hopping to a thread like `sync_to_async` did.

## Trade-offs

- Events are saved at the end of the run, also when it fails (the run's `finally`). A process killed mid-run
  loses that run's events; the answer is lost too, so nothing is hidden.
- The SDK still owns the loop. The harness wraps it through hooks rather than rewriting it.

Source: [The Agent Harness](https://michaellivs.com/blog/agent-harness/), [Context Engineering Has No Engine](https://michaellivs.com/blog/context-engineering-open-call/).
