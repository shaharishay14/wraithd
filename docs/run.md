# One run

**Files:** `run.py`, `config.py`, `providers.py`, `core/compaction.py`, `channels/terminal.py`, `cli.py`

## One-liner

> `run()` takes a message and returns an answer. In between it builds a small context, wires the tools behind the
> gate, runs the loop with its stop rules, and logs every step, including when it fails.

## What a run does

1. **Logs `run_start`.** Everything after this is in the run's event log, saved in the `finally`.
2. **Checks the daily budget** (if one is set) before any model call is paid for. Spent: `refused`, and a
   one-line answer.
3. **Builds the context:** a short base prompt with the date and time, `AGENTS.md` (the owner's standing
   orders), memory L0 and L1, one line per skill, and what `<system-reminder>` notes are. agent-studio's prompt
   was over 17,000 characters before the user said anything; this one starts under 2,000.
4. **Wires the tools:** memory and skill tools for the owner, plus whatever the caller brings (built-in tools in
   Phase 3, MCP tools in Phase 4, discovered past a dozen), all behind one permission gate.
5. **Runs the loop** with a 180-second limit. A failure before anything streamed or ran is retried twice; after
   that it never is.
6. **Logs `run_end`:** answered, refused or failed, with turns, tokens and the stop reason. A failed run answers
   honestly from what the gate saw run: "These actions were already done: ..." or "Nothing was changed."

## Config and keys

`~/.local-agent/config.toml` (or `$LOCAL_AGENT_HOME`) has the model, budgets and the owner's email. Keys never go
there: `local-agent key set <provider>` puts them in the OS keychain (`keyring`), with the provider's
environment variable as a fallback. Every key the process reads is registered for redaction, so it can't reach
the model or the log even if a tool echoes it.

## Providers

| `provider` | How | Key |
|---|---|---|
| `openai` | the Responses API | keychain or `OPENAI_API_KEY` |
| `anthropic` | LiteLLM (`local-agent[anthropic]`) | keychain or `ANTHROPIC_API_KEY` |
| `ollama` | Chat Completions on `http://localhost:11434/v1` | none |
| `openai-compatible` | Chat Completions on `base_url` | optional |

LiteLLM is an optional extra: it's large and takes seconds to import, so OpenAI and Ollama users don't pay for
it. When it's used, it reads its bundled price list instead of downloading one, and its telemetry is off.

`default` is the model for every run; `strong` is the one a `model: strong` skill switches to (see
[skills](skills.md)).

## The terminal

`local-agent chat` is one conversation: answers stream as they're written, tool calls show as `· tool_name`,
and a write shows its arguments and warnings, then asks `Allow? [y/N]` (anything but "y" is no). Messages are
stored with the conversation, so a long session is compacted (the newest 10 messages word for word, older ones
folded into a summary in batches of 10). When the session ends, distill turns it into memory.

`local-agent runs list` and `local-agent runs show <id>` (a unique prefix is enough, `--json` for everything)
print what a run did, from its event log.

## On a laptop

- **One small function instead of a service.** agent-studio's `run()` lived in a 2,500-line service with
  Composio loading, intent rules, a chain-of-thought protocol, file attachments and post-run "verification".
  None of it is here: the gate guards actions, the stop rules end bad runs, and the model asks when a request is
  unclear.
- **The owner is the default.** In the terminal the owner is always the one talking; `owner=False` (strangers)
  is for Telegram in Phase 5.
- **Tests run the real `run()`** with a scripted model, against a temporary home and an in-memory keychain.
