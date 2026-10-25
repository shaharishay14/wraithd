# The daemon

**Files:** `daemon/app.py`, `daemon/api.py`, `daemon/security.py`, `daemon/scheduler.py`,
`channels/telegram.py`, `channels/conversation.py`, `tools/notify.py`

## One-liner

> `local-agent daemon` is one async process: the local API and web UI, the scheduler, Telegram long polling, open
> MCP connections and background distill, all sharing one runtime, so every channel sees the same memory, skills,
> approvals and event log.

## What runs in it

- **The API and the web UI** ([api](api.md)), served by uvicorn on `127.0.0.1:7878`, behind one guard: the token,
  the Host check and the Origin check run before any route.
- **The scheduler** ([scheduler](scheduler.md)): every minute, what's due runs unattended, and the answer goes to
  the owner.
- **Telegram** ([telegram](telegram.md)): long polling; the owner's chat, and strangers ignored unless the owner turns them on.
- **MCP connections** ([MCP](mcp.md)): opened once at start and passed to every owner run, instead of a process per
  run. A server that doesn't start is reported at start-up and left out.
- **Background distill**, every hour: any conversation with at least four new messages (web, Telegram, terminal),
  except the notifications inbox, whose messages are the agent's, not the owner's.
- **`notify_owner`**, a built-in tool: the agent messages its owner, on Telegram when a chat is paired, otherwise in
  the "Notifications" conversation the web UI lists. Only the owner receives it, so it needs no approval and works
  unattended.

One message at a time per conversation: a second one waits for the first answer (Telegram), or is refused with
`409` (the web chat). Runs started from a request keep going if the browser goes away.

## Running it

`local-agent daemon` runs in the foreground and prints the UI's address with its token:
`http://127.0.0.1:7878/#token=...`. Ctrl-C stops everything. Installing it as a background service (launchd,
systemd) and `daemon start/stop/logs` are Phase 6.

## On a laptop

agent-studio was a web server with Celery workers, Beat and Redis around it, on a server that never sleeps. Here
it's one process the owner can see and stop. That changes a few things:

- There is no queue: a scheduled run is a task in the same process, and a run is at most once (the schedule moves
  on before it runs), so a crash loses a run rather than repeating it.
- The laptop sleeps. The scheduler fires a slot that was missed during sleep once when it wakes, not once per
  missed slot.
- The API is reachable by every web page the owner opens, through their browser. That's why it has a token, the
  Host and Origin checks, no CORS, and binds to 127.0.0.1 only.
- MCP servers are processes on the laptop: keeping them open for the daemon's life avoids starting one per message.
