# Engines

**Files:** `engines/claude_options.py`, `engines/claude_bridge.py`, `engines/claude_code.py`, `run.py`, `config.py`

## One-liner

> Two engines can run wraithd's thinking loop. `agents-sdk` uses your API key or a local model. `claude-code` runs
> Anthropic's own Claude Code on your Claude Pro or Max login. The gate, approvals, taint, event log and budgets
> work the same on both.

## How they differ

| | `agents-sdk` (default) | `claude-code` |
|---|---|---|
| Thinks with | `[model]`: OpenAI, Anthropic, Ollama, any OpenAI-compatible endpoint | Claude Code on your Claude login |
| Needs | an API key (none for Ollama) | `pip install 'wraithd[claude]'` (~300 MB), then `claude` and `/login` |
| Tools | wraithd's guarded tools | the same guarded tools, as one in-process MCP server; Claude Code's own are off |
| Chat | one run per message | one warm Claude Code process per conversation; `wraithd chat` starts it as it opens, so the ~5 s start overlaps your typing |
| Background work | `[models]` per purpose | `background_model` for the heartbeat, schedules, memory distill and compaction |
| Images | when the model can see | only through an API model; with no key, wraithd says it can't read images yet |
| Stop rules | the loop's run hooks | checked by the bridge before every tool call; the tool-call limit is counted there too |
| Daily token budget | off unless set | on by default (2,000,000), since your own Claude chats share the plan |

## Config

```toml
[engine]
name = "claude-code"         # or "agents-sdk" (the default)
chat_model = "sonnet"        # chats
background_model = "haiku"   # heartbeat, schedules, distill, compaction
fallback = false             # true: when the plan's limit hits, use [model] instead, if nothing happened yet
idle_minutes = 10            # close a conversation's Claude Code session after this long unused
```

Every Claude Code call names its model. With none set, Claude Code picks Opus, the most expensive.

## The rules

- wraithd never reads `~/.claude/.credentials.json` or Claude Code's keychain entry. It never sets
  `CLAUDE_CODE_OAUTH_TOKEN` and never calls Anthropic's API with a login token. Only the unmodified Claude Code
  program, started by `claude-agent-sdk`, talks to Anthropic.
- `claude_options()` is the only place Claude Code's options are built. It always sets:
  - `tools=[]`, `strict_mcp_config=True` and `setting_sources=[]`: none of Claude Code's own tools, MCP servers,
    settings or hooks;
  - `env={'ANTHROPIC_API_KEY': ''}`: the login, never a key;
  - `--no-session-persistence`: no conversation transcripts or project files are left behind (nothing under
    `~/.claude/projects`). Claude Code's own bookkeeping (`~/.claude.json`, its plugin cache, temporary session
    files) is expected.

  The doctor and the audit use the same function.
- Claude Code must report `apiKeySource: none`, the login. Bedrock and Vertex report `none` too, so
  `CLAUDE_CODE_USE_BEDROCK`, `_VERTEX` or `_FOUNDRY` in the environment refuses the run
  (`subscription_env_problem()`).
- **Fallback** happens only when all of these hold:
  - `fallback = true`;
  - an API model or Ollama is set up;
  - no write has completed;
  - no text has streamed to you.

  Otherwise the run stops with a plain line: what is wrong (the plan's limit, which resets later, or the wrong
  login) and any actions already done.

## Warm sessions

- Each conversation keeps one warm session. Its tools, system prompt and model are fixed when it connects, so a
  run that needs different ones gets a new session.
- `wraithd chat` (rich and plain) starts its conversation's session as soon as it opens (`run.start_warm_session`,
  a background task), so Claude Code starts while you type. It builds the context exactly as the first message's
  `run()` does (`run(..., warm_up=True)` stops after the prompt and tools and saves no run), so the first message
  reuses that session instead of opening a second one. That message waits for the start if it isn't done (the
  conversation's lock). If the early start fails, the first message starts the session as before and shows any
  problem then. Quitting before the first message cancels the start and closes the session.
- A new session gets wraithd's compacted history as a transcript. A known session gets only the new message, the
  time and any reminders.
- A `RunSlot` holds the guarded tools of the run that is answering. Between runs it is empty, and any call is
  refused.
- Third-party text read by one message taints the conversation's next messages too. It is passed in as
  `inherited_taint` before the gate is built.
- Sessions close:
  - after any error;
  - after `idle_minutes` unused (the pool's own reaper);
  - when chat ends (`/quit`, Ctrl-D, Ctrl-C) or the daemon stops: closed and awaited before the event loop ends;
  - otherwise when wraithd's event loop ends (the reaper's last step).
- Every call closes the SDK's message generator (`query()`, `receive_response()`) itself before it returns. Left
  open, the event loop closes the SDK's nested generators all at once when it ends, and one of them is still closing:
  "aclose(): asynchronous generator is already running". `close()` cancels a message still answering and waits for
  it before it disconnects.
- Each Claude Code start is an `engine_connect` event (`kind`: `session` for a warm session's connect, `once` for a
  one-shot's start until its init message; `ms`). `run_end`'s `duration_ms` still counts the whole run. A session
  started early logs it on its first message with `early: true`; `ms` is still Claude Code's own start (the early
  connect plus the wait for init), not the time you spent typing.

## Checked how

`tests/engines/` runs fully offline against a fake `claude_agent_sdk`. No Claude Code process starts and the real
login and home folder are never touched. The tests cover:
- the options;
- the bridge, including run 2 never using run 1's gate;
- a chat that asks approval for a write, with `wraithd runs show` listing every call;
- the three fallback cases;
- warm sessions: reuse, taint carried over, changed tools, the idle reaper, ordering, shutdown;
- quitting chat with an open session prints nothing (the fake SDK nests its generators like the real one);
- `engine_connect` with its ms;
- the early start in chat (`test_warm_start.py`): the first message reuses it (one connect), a failed start falls
  back, quitting or Ctrl-C while it starts is clean, and the first-message wait before and after. With a 2 s start
  (`WRAITHD_MEASURE_SCALE=1 uv run pytest tests/engines/test_warm_start.py -s -k measure`), message sent to first
  token: a fast typist (sends at 0.5 s) 2.01 s before, 1.50 s after; a slow one (sends at 3 s) 2.01 s before,
  0.005 s after.

`scripts/spike_claude.py` is the one real check, on your own login: `auth: none` and `echo: hi`.
