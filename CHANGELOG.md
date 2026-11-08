# Changelog

## 0.1.0 (unreleased)

### Run on your Claude subscription
- **A second engine: Claude Code on your own Claude Pro/Max login, no API key.** Pick "Claude subscription" in
  `wraithd onboard`, run `claude` and type `/login` yourself. wraithd never reads, stores or forwards the login:
  it starts Anthropic's unmodified Claude Code (the optional extra: `uv tool install 'wraithd[claude]'`) and hands
  it only wraithd's own gated tools. Approvals, taint tracking, the event log and budgets work as on the API engine.
  API keys and local models stay supported. See [docs/claude-subscription.md](docs/claude-subscription.md).
- Chat runs on Sonnet; heartbeat, schedules, memory and summaries on Haiku (`[engine] chat_model`,
  `background_model`). The daily token budget is on by default in this mode.
- One warm Claude Code session per conversation, started as `wraithd chat` opens, so the first answer doesn't wait
  for Claude Code to start. Idle sessions close after 10 minutes (`[engine] idle_minutes`).
- Plan limit: falls back to your API key or local model only if you turned `[engine] fallback` on, nothing was
  written and nothing was shown yet; otherwise it stops and says so.
- `wraithd doctor` checks the login with one short message; `wraithd audit` checks Claude Code gets no tools or
  settings of its own, refuses Bedrock/Vertex/Foundry, and fails on any Claude login token held by wraithd.
- No conversation transcripts are left in `~/.claude`.

### A friendlier terminal
- Pickers instead of typing (arrow keys and Enter, number keys 1-9) in onboarding, approvals, and commands that
  need an id (`schedules rm`, `runs show`, `key rm`, `review`, ...).
- Chat: live Markdown answers, ↑ history, Esc+Enter for a new line, `/help /new /runs /schedules /model /clear`
  with Tab, a spinner per tool with ✓ / ✗ (including tool errors), a boxed approval card with its warnings on top.
- Tables for runs, schedules, events, doctor and audit. A black-and-white look with a violet accent, and the
  wraithd icon when onboarding opens.
- Plain output whenever it's piped, with `NO_COLOR`, `--plain` or `WRAITHD_PLAIN=1`: exactly as before, so scripts
  keep working.
- New core dependencies: `rich`, `prompt-toolkit`.

### Heartbeat and notifications
- Lean heartbeat: wraithd looks up today's schedules and reminder notes itself and asks the model once, with no
  tools: about 1.4k tokens instead of ~11k. It runs the full heartbeat with tools when an email or calendar app is
  connected or you wrote a `HEARTBEAT.md`.
- "Already told them lately" now includes schedule answers and `notify_owner` messages, so a heartbeat doesn't
  repeat them. Everything Telegram delivers is also kept once in the Notifications inbox.

### Schedules
- A scheduled run whose answer is exactly `NO_REPLY` sends nothing, like the heartbeat's `HEARTBEAT_OK`: a skill
  that already sent its items with `notify_owner` (or found nothing) ends that way, with no extra "⏰" message.

### Feed watches
- `[watch.<name>]` checks a JSON feed in code every few minutes (url, items, key, every_minutes, message) and runs
  the agent only when there are new items, so a quiet poll costs no model tokens. The first poll only records what's
  there. New items are third-party input (the run is tainted). `wraithd watch` lists watches; `wraithd watch now
  <name>` polls one now.

### Telegram
- `/mcp_add` adds an MCP server from your own Telegram chat: you confirm the exact command or URL, its tools are
  pinned on first connect, and secrets are never accepted in the chat (env file or a 1Password reference only).
  Only you can use it, and the model can't.

### Skills and the web
- `read_website` also returns the page's links (and RSS/Atom items' links), so a skill can link the story or the
  posting itself instead of a homepage.
- On the Claude subscription, a scheduled (or watch) run that names a `model: strong` skill runs on `chat_model`;
  other scheduled runs use `[engine] schedule_model` (Haiku unless set).

### Smaller
- `wraithd runs show` prints `engine_connect` (how long Claude Code took to start).
- Quitting chat no longer prints an `aclose()` error.
- `wraithd mcp add --env NAME` with no terminal to type the secret says to use `NAME=op://...` or the environment
  variable, instead of a traceback.

## 0.0.1 (2026-11-03)
- First release on PyPI.
