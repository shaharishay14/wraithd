# The daemon

**Files:** `daemon/app.py`, `daemon/api.py`, `daemon/security.py`, `daemon/scheduler.py`, `daemon/service.py`,
`daemon/jobs.py`, `channels/base.py`, `channels/telegram.py`, `channels/conversation.py`, `tools/notify.py`

## One-liner

> `local-agent daemon` is one async process: the local API and web UI, the scheduler, Telegram long polling, open
> MCP connections and background distill, all sharing one runtime, so every channel sees the same memory, skills,
> approvals and event log.

## What runs in it

- **The API and the web UI** ([api](api.md)), served by uvicorn on `127.0.0.1:7878`, behind one guard: the token,
  the Host check and the Origin check run before any route.
- **The scheduler** ([scheduler](scheduler.md)): every minute, what's due runs unattended, and the answer goes to
  the owner.
- **Telegram** ([telegram](telegram.md)): long polling; the owner's chat only; anyone else is ignored. It's the
  first channel on the shared channel interface ([channels](channels.md)).
- **MCP connections** ([MCP](mcp.md)): opened once at start and passed to every owner run, instead of a process per
  run. A server that doesn't start is reported at start-up and left out.
- **Log rotation**, every minute (see "The log" below).
- **Background distill**, every hour: any conversation with at least four new messages (web, Telegram, terminal),
  except the notifications inbox, whose messages are the agent's, not the owner's.
- **`notify_owner`**, a built-in tool: the agent messages its owner, on Telegram when a chat is paired, otherwise in
  the "Notifications" conversation the web UI lists. Only the owner receives it, so it needs no approval and works
  unattended.

One message at a time per conversation: a second one waits for the first answer (Telegram), or is refused with
`409` (the web chat). Runs started from a request keep going if the browser goes away.

## Running it

`local-agent daemon` runs in the foreground and prints the UI's address with its token:
`http://127.0.0.1:7878/#token=...`. Ctrl-C stops everything. `local-agent daemon install` runs it as a background
service instead (launchd or systemd), with `daemon start/stop/status/logs`: see [installing and running](install.md).
To reach it from your phone or another computer, put `tailscale serve` in front of it ([remote access](remote-access.md)).

## The log

The service appends the daemon's output to `~/.local-agent/logs/daemon.log`. The daemon rotates it itself, at
start and every minute (a job in `daemon/jobs.py`):

- Past **5 MB** it's copied to `daemon.log.1`, and the older copies move to `.2` and `.3`. The oldest is dropped,
  so the log takes at most about 20 MB. Copies are mode 600 in the private `logs/` folder.
- Then it's emptied **in place**, not renamed: launchd and systemd keep the file open in append mode, so a renamed
  file would keep growing under its new name. After emptying, their next write lands at the start of the same file.
- **A few lines can be lost.** Lines written between the end of the copy and the emptying are in neither file. The
  window is a few milliseconds every 5 MB, and the log is mostly warnings, but nothing guarantees every line.
- `local-agent daemon logs -n N` reaches into `.1`, `.2`... when the current file is shorter than N lines, and
  `-f` keeps following through a rotation (it starts again at the top of the emptied file).

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
