# Remote access

**Files:** `daemon/security.py` (Host and Origin), `daemon/app.py`, `daemon/commands.py` (`daemon status`), the
`[remote]` section in `config.py`

## One-liner

> To use the agent from a phone or another computer, the API stays on 127.0.0.1 and `tailscale serve` puts it on
> the owner's tailnet with HTTPS. The daemon lets in exactly that one name, and the token is still required.

## Why this shape

The API is built for one browser on one machine ([api](api.md)). It binds to 127.0.0.1, checks Host against DNS
rebinding and checks Origin against cross-site requests. Binding to `0.0.0.0` would put it on every network the
machine joins (a café's Wi-Fi, a hotel's), so remote access doesn't change the bind. Instead:

- **Tailscale** carries the traffic. It's WireGuard between the owner's own devices, so nothing is on the public
  internet and there's no port forwarding.
- **`tailscale serve`** listens on the machine's tailnet address on 443, with a real certificate for its
  `*.ts.net` name, and passes each request on to `127.0.0.1:7878`. It keeps the request's `Host`, so the daemon
  sees `box.tail1234.ts.net`.
- **`[remote] allowed_hosts`** tells the daemon that name is its own. It adds one more allowed Host and one more
  allowed Origin (`https://box.tail1234.ts.net`). Nothing else gets in.

## Setup on an always-on Linux box

1. Install Tailscale and log in. `tailscaled` runs as a system service, so it's up after a reboot:
   ```
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   tailscale status            # this machine's name, e.g. box.tail1234.ts.net
   ```
   MagicDNS and HTTPS certificates must be on for the tailnet (admin console → DNS → *HTTPS Certificates*).
2. Let the agent run without anyone logged in. A systemd **user** unit stops at logout unless lingering is on:
   ```
   sudo loginctl enable-linger $USER
   local-agent daemon install
   ```
3. Tell the daemon its tailnet name, in `~/.local-agent/config.toml`:
   ```toml
   [remote]
   allowed_hosts = ["box.tail1234.ts.net"]
   ```
   Then `local-agent daemon restart`. An entry is a bare host name (optionally `:port`). `*`, `https://...`, a path or
   a name without a dot are refused when the config loads.
4. Put the API on the tailnet:
   ```
   tailscale serve --bg 7878          # https://box.tail1234.ts.net → http://127.0.0.1:7878
   tailscale serve status
   ```
   `--bg` keeps the setting across reboots. `tailscale serve reset` removes it.
5. `local-agent daemon status` prints the link with the token in its fragment:
   `https://box.tail1234.ts.net/#token=...`. Open it once on the phone (with Tailscale on). The UI keeps the token
   and removes it from the address bar.

On a Mac it's the same, except steps 1 and 2: use the Tailscale app, and the launchd agent runs while the owner is
logged in.

**Never `tailscale funnel`.** Funnel publishes the name on the public internet. The token would be the only lock,
and the "no public URL" rule would be gone.

## What's checked

| Request | Answer |
|---|---|
| Host `box.tail1234.ts.net` (or `:443`), the token | served |
| Host `box.tail1234.ts.net`, no token or a wrong one | `401` |
| Origin `https://box.tail1234.ts.net` | allowed |
| Origin `http://box.tail1234.ts.net`, `https://sub.box.tail1234.ts.net`, another tailnet machine's page | `403 bad_origin` |
| Host of another machine or name, a rebinding name | `403 bad_host` |
| Host `127.0.0.1:7878` | as before |

The tests (`tests/reach/test_remote.py`, `tests/attacks/test_reach_remote.py`) cover each row. `local-agent audit`
lists every remote name as reachable and fails on one that isn't a `*.ts.net` name or is an IP address
(`daemon.security.remote_hosts(config)`).

## Limits

- Any device on the tailnet that the tailnet's access rules let through can reach the port. The token is what
  stops it, so keep it private, and rotate it (`rm ~/.local-agent/api-token`, restart) if a device is lost.
- The daemon doesn't use Tailscale's identity headers (`Tailscale-User-Login`). The token is the one credential,
  the same as on the laptop.
- If `tailscale serve` were set up to rewrite Host, every request would get `bad_host`. That fails closed, never
  open. `curl -sI https://box.tail1234.ts.net/api/health` should answer `200`.
