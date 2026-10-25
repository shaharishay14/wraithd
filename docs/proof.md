# Proof that it works

**Files:** `core/replay.py`, `tests/`, `tests/scripted.py`, `tests/core/runs/`

## One-liner

> The harness is tested where it's deterministic, run end to end through the real SDK loop with a scripted
> model, and replayed from recorded runs. No test calls a model, needs a key or touches the network.

## Three layers of tests

1. **Unit tests** for the deterministic parts: views, stop rules, reminders (priority, once per run), the
   gate's classification and taint rules, disclosure order, egress, redaction, the store.
2. **Runs through the real SDK loop** (`tests/scripted.py`). `ScriptedModel` stands in for the LLM inside the
   real Agents SDK Runner: it plays back canned turns (tool calls, text, or an exception), streams text in
   pieces like a real model, reports token usage, and records what the Runner sent it. `run_agent` wires the
   harness the way the real `run()` will: a gate, an event log, tool-bound reminders, the loop. So a test can
   say "the model calls the broken tool three times" and check the stop, the final instruction and the saved
   timeline, without cost.
3. **Replay** (`core/replay.py`): recorded runs (`tests/core/runs/*.json`) go back through today's stop rules
   and reminders. If a rule change alters a decision, the diff names the event:
   `#15: recorded stop tool_errors, now no stop`. Save any run as a fixture with
   `replay.dump(store.run_events(run_id), path)`.

Golden evals with a real model in a fake world come in Phase 8; they're opt-in, never part of `pytest`.

## On a laptop

- **No Django test database.** Every test that needs storage gets a fresh in-memory `SQLiteStore`.
- **The fixtures came from agent-studio.** The four recorded runs were made there by real runs of the same
  harness, so replaying them here checks the port decides exactly as the original did. Only the model's
  printed object address was replaced with `"scripted"`. They'll be re-recorded from local runs once `run()`
  exists (Phase 2).
- **Tests don't depend on the developer's machine.** Keys in the environment are cleared for every test, so a
  real `OPENAI_API_KEY` can't change what gets redacted, and the egress tests resolve made-up hostnames through
  a fake DNS and fetch through `httpx.MockTransport`.

Source: [Your Eval Sucks and Nobody Is Coming to Save You](https://michaellivs.com/blog/your-eval-sucks/).
