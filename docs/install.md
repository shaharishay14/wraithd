# Installing and running

**Files:** `scripts/release.sh`, `onboard.py`, `daemon/service.py`, `daemon/commands.py`, `doctor.py`, `update.py`

## One-liner

> One package, one wizard: `uv tool install local-agent`, then `local-agent onboard` asks for a model and key,
> sets up Telegram, installs a background service that starts at login, and prints the web UI's link.

## How it works

- **The release build** (`scripts/release.sh`): `npm ci` and `npm run build` on a temp copy of `frontend/` (never
  in the checkout), the result copied into `src/local_agent/ui/`, then `uv build`. That folder is gitignored and
  shipped by hatch as an artifact, so the wheel carries the UI while git doesn't. The script checks the wheel for
  `local_agent/ui/index.html` and fails loudly on anything missing (node, npm, uv, the lockfile). Without a UI build
  the wheel still builds, and the daemon serves a placeholder page. CI's `wheel` job runs the script, installs the
  wheel as a uv tool in a temp folder, and checks the no-UI build too. Publishing (`uv publish`) is the owner's step.
- **Install:** `uv tool install local-agent` or `pipx install local-agent`. Both give the command its own
  environment; nothing goes into the system Python.
- **`local-agent onboard`** asks, in order: the provider and models, the key (typed without echo, into the
  keychain) and whether to check it with one request for the provider's model list, an optional search key, an
  optional Telegram bot (the token is checked with `getMe` before it's saved), whether to install the background
  service, then pairing (it shows the `/pair` code and waits for it), and ends with a summary and the web UI link.
  - **Safe to run again.** Every question defaults to what's set, so Enter keeps it; a key in the keychain is only
    replaced if the owner says so; a running service is restarted so new settings apply; a paired bot isn't asked
    about. Answers go into `config.toml` line by line (`config.set_values`), keeping the owner's comments, and a
    change that wouldn't load is undone. Ctrl-D stops; what was saved stays.
  - **Pairing without two pollers.** Telegram allows one `getUpdates` caller per bot. If the daemon answers on its
    port, it handles `/pair` and onboard watches the store; otherwise onboard polls once per Enter, handles only
    `/pair`, and leaves later updates for the daemon.
- **The background service** runs `python -m local_agent daemon` with the Python it was installed with (so it
  needs no PATH lookup and survives `update`):
  - macOS: a launchd agent, `~/Library/LaunchAgents/com.local-agent.daemon.plist`, in the owner's GUI session:
    `RunAtLoad` (at login), `KeepAlive` on crashes only, 10-second throttle.
  - Linux: a systemd user unit, `~/.config/systemd/user/local-agent.service`: `Restart=on-failure`,
    `WantedBy=default.target`.
  - Both append stdout and stderr to `~/.local-agent/logs/daemon.log`, and carry `LOCAL_AGENT_HOME`, the owner's
    PATH (launchd and systemd start with a bare one, and MCP servers are often `npx` or `uvx`) and
    `PYTHONUNBUFFERED`. The daemon logs warnings and up with timestamps: httpx logs request URLs at INFO, and
    Telegram's contain the bot token.
  - `local-agent daemon install | start | stop | restart | status | logs [-n N] [-f] | uninstall`. Bare
    `local-agent daemon` still runs in the foreground. `start` installs first if needed. `stop` unloads the service
    (so launchd's KeepAlive doesn't bring it back); it loads again at the next login, and `uninstall` removes it.
    `status` also asks `/api/health` and prints the web UI link.
  - Every `launchctl`/`systemctl` call goes through an injectable runner: tests use a fake service manager and a temp
    HOME, so they never touch the real ones.
- **`local-agent doctor`** reads and changes nothing: Python and package versions, the state folder (mode 700),
  the settings, which keys are present (never their values), the API token file (mode 600), the web UI, the
  service (installed, running), `/api/health` (and whether it's the installed version), the port, and one line for
  `local-agent audit` (the security rules, [From EC2 to laptop](ec2-to-laptop.md)). One line per check, a fix under
  each failure, exit code 1 if anything failed.
- **`local-agent update`** finds the installer from its own environment (`uv-receipt.toml` for uv,
  `pipx_metadata.json` for pipx), runs `uv tool upgrade local-agent` or `pipx upgrade local-agent`, and restarts the
  service if it's installed. Any other install (a checkout, plain pip) is left to the owner rather than guessed at.

## On a laptop

agent-studio was deployed: a server, Celery workers, Redis, a database, all started by the operator. Here the
owner's own account runs everything. The service is a per-user launchd agent or systemd user unit: no root, no
system folders, nothing listening beyond 127.0.0.1. It runs while the owner is logged in; a laptop that sleeps
stops it, and a schedule that came due meanwhile fires once when it wakes (see [the scheduler](scheduler.md)).

## Limits

- On Linux a user unit stops at logout unless lingering is on (`loginctl enable-linger $USER`); onboard doesn't
  turn that on, since it changes a system setting.
- launchd needs a GUI session: installing over SSH fails with launchctl's error, and `local-agent daemon` in a
  terminal (or tmux) is the fallback.
- The log isn't rotated. It's small (warnings only), but it grows.
- Windows has no service; `local-agent daemon` works in a terminal.
