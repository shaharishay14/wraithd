# Proof that it works

**Files:** `core/replay.py`, `tests/`, `tests/scripted.py`, `tests/core/runs/`, `tests/core/record_runs.py`

## One-liner

> The harness is tested where it's deterministic, run end to end through the real SDK loop with a scripted
> model, and replayed from recorded runs. No test calls a model, needs a key or touches the network.

## Three layers of tests

1. **Unit tests** for the deterministic parts: views, stop rules, reminders (priority, once per run), the
   gate's classification and taint rules, disclosure order, egress, redaction, the store.
2. **Runs through the real SDK loop** (`tests/scripted.py`). `ScriptedModel` stands in for the LLM inside the
   real Agents SDK Runner: it plays back canned turns (tool calls, text, or an exception), streams text in
   pieces like a real model, reports token usage, and records what the Runner sent it (input, tools,
   instructions). Tests call the real `run()`, and the real terminal chat with a scripted owner. So a test can
   say "the model calls the broken tool three times" and check the stop, the final instruction and the saved
   timeline, or "I prefer short replies, quit, start again" and check the next prompt, without cost.
3. **Replay** (`core/replay.py`): recorded runs (`tests/core/runs/*.json`) go back through today's stop rules
   and reminders. If a rule change alters a decision, the diff names the event:
   `#15: recorded stop tool_errors, now no stop`. The fixtures are recorded by real runs:
   `RECORD_RUNS=1 uv run pytest tests/core/record_runs.py`, after a deliberate behaviour change. Any run can be
   saved the same way with `replay.dump(store.run_events(run_id), path)`.

Golden evals with a real model in a fake world are opt-in and never part of `pytest`, which runs only their
scripted version ([Evals](evals.md)).

## On a laptop

- **No Django test database, no real home.** Every test gets a temporary `LOCAL_AGENT_HOME` and an in-memory
  keychain, so nothing touches `~/.local-agent` or the OS keychain.
- **The fixtures were re-recorded locally.** Phase 1 replayed agent-studio's four recorded runs to show the
  port decided exactly as the original. Phase 2 re-recorded them through the local `run()`: the decisions (every
  stop, every reminder, at the same events) are unchanged; the context around them is smaller.
- **Tests don't depend on the developer's machine.** Keys in the environment are cleared for every test, so a
  real `OPENAI_API_KEY` can't change what gets redacted, and the egress tests resolve made-up hostnames through
  a fake DNS and fetch through `httpx.MockTransport`.

Source: [Your Eval Sucks and Nobody Is Coming to Save You](https://michaellivs.com/blog/your-eval-sucks/).
