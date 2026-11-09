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
| Chat | one run per message | short messages on `quick_model` (Haiku), the rest on `chat_model` (Sonnet); one warm Claude Code process per conversation and model; `wraithd chat` starts both as it opens, so the ~5 s start overlaps your typing |
| Background work | `[models]` per purpose | `background_model` for the heartbeat, schedules, memory distill and compaction; `schedule_model` for schedules when set; a schedule naming a `model: strong` skill on `chat_model` |
| Images | when the model can see | only through an API model; with no key, wraithd says it can't read images yet |
| Stop rules | the loop's run hooks | checked by the bridge before every tool call; the tool-call limit is counted there too |
| Daily token budget | off unless set | on by default (2,000,000), since your own Claude chats share the plan |

## Config

```toml
[engine]
name = "claude-code"         # or "agents-sdk" (the default)
chat_model = "sonnet"        # chats
quick_model = "haiku"        # short chat messages; "" puts every chat on chat_model
background_model = "haiku"   # heartbeat, schedules, distill, compaction
schedule_model = ""          # scheduled runs; "" means background_model
fallback = false             # true: when the plan's limit hits, use [model] instead, if nothing happened yet
idle_minutes = 10            # close a conversation's Claude Code session after this long unused
```

Every Claude Code call names its model. With none set, Claude Code picks Opus, the most expensive.

### A chat message's model

Warm Sonnet turns take ~2.5-4.5 s to the first token even for "What's 12 times 12"; wraithd's own share is
10-40 ms. Haiku answers short questions much sooner, so a chat message goes to `quick_model` when all of these hold:

- no attachment (photo, voice note or file) and it isn't the how after a draft's Rewrite tap;
- at most 200 characters (`run.QUICK_MAX_CHARS`);
- it names no skill (any offered skill's name as a whole word, so a `model: strong` one too);
- no approval card waits on its channel, and it isn't a `/command`;
- it doesn't start with `think` or `/sonnet`.

Anything else is on `chat_model`. `think ...` or `/sonnet ...` at the start is how you ask for Sonnet on a short
message; the word is taken off what the model sees (`run_start` keeps what you typed). `quick_model = ""` (or the
same as `chat_model`) turns this off. Only chats: the heartbeat, schedules, memory and summaries are picked as
below. `context_built` has the choice: `model`, and `model_reason`: `quick`, or why it's on `chat_model`
(`prefix`, `attachment`, `rewrite`, `approval`, `command`, `skill`, `length`).

### A scheduled run's model

Claude Code fixes its model when it connects, so the API engine's mid-run switch to the strong model (after a
`model: strong` skill loads, see [skills.md](skills.md)) can't happen here. wraithd picks before the run starts:

1. The schedule's message names a `model: strong` skill (yours or a built-in) as a whole word, e.g.
   `run news-feed (scheduled)`: the run is on `chat_model` from the start.
2. Otherwise `schedule_model`, when set.
3. Otherwise `background_model`, as before.

Feed-watch runs (source `watch`, unattended like a schedule) are picked the same way. Nothing else moves a
scheduled or watch run off `background_model`, so it never costs more by surprise. The heartbeat,
memory distill and compaction always use `background_model`. `wraithd runs show` has the choice on
`context_built`: `model`, and `model_reason` (`skill news-feed is model: strong`, `schedule_model` or
`background_model`).

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

- Each conversation keeps one warm session per model: with `quick_model` on, up to two (`chat_model`'s and
  `quick_model`'s), in `SessionPool.sessions` by `(conversation, model)`. A session's tools, system prompt and
  model are fixed when it connects, so a run that needs different tools or prompt replaces that model's session.
  Each model connects once per conversation; the system prompt is the same for both.
- The other model's session starts in the background once a message has answered, if it has none
  (`SessionPool.prestart`): Telegram and the web UI have no chat-opens moment, so the first message starts its own
  model's session and the second model is ready by the next message. It doesn't hold the conversation, so a
  message on the first model never waits for it.
- `wraithd chat` (rich and plain) starts its conversation's sessions (both models, side by side) as soon as it opens (`run.start_warm_session`,
  a background task), so Claude Code starts while you type. It builds the context exactly as the first message's
  `run()` does (`run(..., warm_up=True)` stops after the prompt and tools and saves no run), so the first message
  reuses that session instead of opening a second one. That message waits for the start if it isn't done (the
  conversation's lock). If the early start fails, the first message starts the session as before and shows any
  problem then. Quitting before the first message cancels the start and closes the session.
- `/new` does the same for the next conversation: it cancels any start still under way, closes the old
  conversation's session, starts the new conversation and its session in the background, then distills the old
  one while Claude Code starts. The new conversation's first message waits for it as on open.
- A new session gets wraithd's compacted history as a transcript. A known session gets only the new message, the
  time and any reminders.
- A `RunSlot` holds the guarded tools of the run that is answering. Between runs it is empty, and any call is
  refused.
- Third-party text read by one message taints the conversation's next messages too, on either model: taint is kept
  per conversation, not per session. It is passed in as `inherited_taint` before the gate is built.
- What a second Claude Code process costs: an idle Claude Code process measured 60-250 MB resident on macOS (the
  owner's own sessions, `ps -o rss`); expect about 150-250 MB on a Linux server. Two per open conversation fit a
  2 GB server for one owner's terminal and Telegram chats; `idle_minutes` closes both when the conversation goes
  quiet, and `quick_model = ""` keeps it at one.
- Sessions close:
  - after any error (that model's session only);
  - after `idle_minutes` unused (the pool's own reaper): a conversation's sessions close together, once none was
    used for that long;
  - when chat ends (`/quit`, Ctrl-D, Ctrl-C) or the daemon stops: closed and awaited before the event loop ends;
  - otherwise when wraithd's event loop ends (the reaper's last step).
- Every call closes the SDK's message generator (`query()`, `receive_response()`) itself before it returns. Left
  open, the event loop closes the SDK's nested generators all at once when it ends, and one of them is still closing:
  "aclose(): asynchronous generator is already running". `close()` cancels a message still answering and waits for
  it before it disconnects.
- One-shots (`run_once`, doctor's login check, the search helper) iterate the SDK client's `process_query()`
  (`claude_code.one_shot`): the public `query()` only wraps it and leaves it for the event loop to close later, so
  the Claude Code process could still be closing when `asyncio.run()` closes the loop ("RuntimeError: Event loop is
  closed" from `BaseSubprocessTransport.__del__` as `wraithd doctor` exits). After each one, `close_leftover_pipes()`
  closes the pipes of an exited Claude Code that a child of it still holds open, while the loop still runs.
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
- a clean exit (`test_clean_exit.py`, the real SDK against a fake `claude` program, `fake_claude_cli.py`): doctor's
  check and a one-shot leave no Claude Code transport open when the loop ends, and stderr has no "Event loop is
  closed", also when a child of `claude` holds its stdout;
- `/new`: one connect for the new conversation, the old session closed, its first message reuses the early start,
  `/quit` right after `/new` is clean;
- the quick model (`test_quick_model.py`): a short question on the quick session, `think` and `/sonnet` on Sonnet
  without the prefix, a skill name, a long message and a waiting approval on Sonnet, one connect per model, the
  other model's session started after the first answer, taint from a Sonnet turn in the next quick turn and the
  other way, a failed turn dropping only its own session, both closing together on idle, `/quit` and `/new`;
- web search through the subscription (`test_claude_search.py`): the helper's options, parsing, errors, the audit
  rule, and a chat whose search comes back tainted and counted;
- the early start in chat (`test_warm_start.py`): the first message reuses it (one connect), a failed start falls
  back, quitting or Ctrl-C while it starts is clean, and the first-message wait before and after. With a 2 s start
  (`WRAITHD_MEASURE_SCALE=1 uv run pytest tests/engines/test_warm_start.py -s -k measure`), message sent to first
  token: a fast typist (sends at 0.5 s) 2.01 s before, 1.50 s after; a slow one (sends at 3 s) 2.01 s before,
  0.005 s after.

`scripts/spike_claude.py` is the one real check, on your own login: `auth: none` and `echo: hi`.
