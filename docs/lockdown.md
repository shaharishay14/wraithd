# Lockdown: keeping wraithd away from what you don't want it to touch

**For:** the owner, running wraithd on their own Mac with one calendar and Telegram ([first launch](first-launch.md)),
who wants it to reach as little as possible. **Files:** `config.py`, `mcp/config.py`, `mcp/client.py`, `run.py`,
`audit.py`, `core/gate.py`

## One-liner

> Give it its own macOS user so the operating system keeps it out of your files, turn off every tool you don't
> need (shell, web, browser), offer only the calendar tools you named, approve every write yourself, cap the
> spend, and check it all with `wraithd audit`. Then know what that still doesn't cover.

## What it can reach, before and after

| | Default | After lockdown |
|---|---|---|
| Your files | only `~/.wraithd/workspace/files/` through its file tools ([files](files.md)); the process itself runs as you | the same, and the process runs as another macOS user who can't open your folders |
| Shell commands | off (`[shell] enabled = false`) | off |
| Web search, opening pages | on | off (`[web] search = ""`, `[web] read = false`) |
| Headless browser, subagents, heartbeat | off | off, written down so nobody turns them on by accident |
| Voice notes | sent to OpenAI for transcription | off |
| The calendar server | every tool it lists; reads run, writes ask | only the tools you name (`--tools`); reads run, creating an event asks every time |
| Telegram | your paired chat only; strangers get silence ([telegram](telegram.md)) | the same |
| Spend | 200k tokens and 40 tool calls per run, no daily cap | 100k and 20 per run, 1M tokens a day |

## 1. A separate macOS user (the strongest layer)

Everything else on this page is wraithd's own code saying no. A second macOS user is the operating system saying
no: if wraithd, the calendar server or a library they load has a bug, the process still can't open your Documents,
Desktop, Downloads, Mail, Photos, your browser's cookies, your SSH keys or your keychain.

1. **System Settings → Users & Groups → Add User.** New account: **Standard** (not Administrator). Name it
   `wraithd`. Give it its own password, not yours.
2. **Close your home folder to other users.** On macOS your home folder is readable by other local users by default
   (`drwxr-x---`, group `staff`), though Desktop, Documents, Downloads and Library inside it are not. In your own
   account:

   ```sh
   ls -ld ~             # drwxr-x---+ ... staff  <- the "r-x" in the middle is what other users get
   chmod 700 ~
   ls -ld ~             # drwx------+
   ```

   This also closes your `~/Public` drop box to other users of this Mac. Undo with `chmod 750 ~`.
3. **Log in as `wraithd`** (Control Center → your name → `wraithd`, so your own session stays open) and do the whole
   [first launch](first-launch.md) there: uv, wraithd, `onboard`, the calendar login. Its keys go in the `wraithd`
   user's login keychain and its Google token in `/Users/wraithd/.config/`, not in yours.
4. **Check it** from a terminal in the `wraithd` account:

   ```sh
   ls ~<you>/Documents                # Permission denied
   ls ~<you>                          # Permission denied (after step 2)
   ```

5. **Switch back to your account; don't log `wraithd` out.** The background service is a launchd agent in that
   user's login session ([install](install.md)): it runs while the session is open, also while you use your own.
   After a restart, log in to `wraithd` once (then switch back) to start it again.

What it costs: a second login after each restart, and installing uv and wraithd once more for that user. Node from nodejs.org's
installer (in `/usr/local/bin`) or Homebrew's (in `/opt/homebrew/bin`) works for both users; uv installs per user.
Don't grant the `wraithd` user Full Disk Access, Accessibility or any other privacy permission in System Settings.

**Can't use a second user?** Everything below still holds in your own account, but the fences are then only
wraithd's code: a bug in it, or in the calendar server, runs with your full permissions.

## 2. The settings: one block

After `wraithd onboard`, open `~/.wraithd/config.toml` and replace everything **above** the `[mcp.servers.calendar]`
block (if you've added it already) with this. Keep your own `[model]` lines if onboard wrote different ones, and
your timezone:

```toml
[model]
provider = "openai"
default = "gpt-4o-mini"
strong = "gpt-4o"

[budget]
run_tokens = 100000          # per run; a runaway run stops here
run_tool_calls = 20          # per run
daily_tokens = 1000000       # across the day; about $0.15-0.60 on gpt-4o-mini

[owner]
timezone = "Europe/London"   # your timezone, for the morning summary

[web]
search = ""                  # no web search
read = false                 # no opening web pages

[voice]
transcribe = ""              # voice notes off (they'd go to OpenAI)

[shell]
enabled = false              # no shell commands at all

[browser]
enabled = false              # no headless browser

[subagents]
enabled = false

[heartbeat]
enabled = false              # nothing runs on its own except schedules you approved

[sandbox]
backend = "none"             # only matters for the shell, which is off

[daemon]
port = 7878                  # the API and web UI, on 127.0.0.1 only
```

Then `wraithd daemon restart` and `wraithd audit` (below). A typo stops the daemon from starting rather than
loosening anything: `wraithd doctor` names the line.

What each line takes away:

- **`[web] search = ""` and `read = false`:** the model can't search or open any URL. Calendar text (an invite from
  someone else) can't make it "visit" a link that carries your data out. wraithd has no per-site allow-list for
  pages; off is stronger than one.
- **`[shell] enabled = false`:** the default, written down. With it on, every command asks first and never runs
  unattended ([shell](shell.md)), but a command you approve runs with the user's full permissions.
- **`[voice] transcribe = ""`:** a Telegram voice note gets a reply saying voice is off, instead of audio going to
  a transcription service.
- **Budgets:** checked before every model call; a run that hits one stops, and so does the day. Also set a monthly
  limit on the provider's side (platform.openai.com → Limits) and use a key made just for wraithd, so you can
  revoke it without touching anything else.

## 3. The calendar server: only the tools you name

Two lists, one on each side, so a mistake in one is caught by the other:

```sh
TOOLS=list-calendars,list-events,get-event,search-events,get-freebusy,get-current-time,create-event
wraithd mcp add calendar --provides calendar --tools $TOOLS \
  --env GOOGLE_OAUTH_CREDENTIALS=$HOME/.config/google-calendar-mcp/gcp-oauth.keys.json \
  -- npx -y @cocal/google-calendar-mcp@2.8.1 start --enable-tools $TOOLS
```

- **`--enable-tools`** is the server's own switch: tools not on it are never registered, so they don't exist.
  The server always adds `manage-accounts` (it adds and removes Google accounts).
- **`--tools`** is wraithd's: only those tools are ever offered to the model, whatever the server lists, so
  `manage-accounts` is hidden too. It's stored as `tools = [...]` in the server's block. `wraithd mcp tools calendar`
  shows the rest as `not offered`, and `wraithd mcp allow` refuses them.
- **Left out on purpose:** `update-event` and `delete-event` (the server marks them destructive, so they'd ask every
  time anyway), `respond-to-event` (answers invites in your name), `create-events` (many at once), `manage-accounts`.
- **Fully read-only:** drop `create-event` from both lists. The server has no read-only mode of its own, and its
  Google login asks for read and write access to your calendars, so the lists are what keeps it read-only, not
  Google.
- **`@2.8.1`** pins the version: `npx` won't fetch a newer one on its own. Upgrading is a decision: change the
  version, then `wraithd mcp tools calendar`; any tool whose description or schema changed asks again until you
  review it (`wraithd review list`, [MCP](mcp.md#pinning-a-tool-is-what-it-was-when-you-trusted-it)).
- **Never `wraithd mcp allow calendar create-event`.** Without it, every event asks you on Telegram with
  **Allow** / **Deny**, and a scheduled run (nobody there to ask) can only read.

What it touches: your Google calendars, through Google's API with the OAuth token in
`~/.config/google-calendar-mcp/tokens.json` (a plain file, readable by the user it belongs to: one more reason for
the separate user). The process gets only `PATH`, `HOME` and its own variable, never the model or Telegram keys
([MCP](mcp.md#secrets)).

## 4. What asks and what doesn't

| Runs without asking | Asks you every time | Never runs |
|---|---|---|
| calendar reads (`list-events`, `get-event`...) | `create-event` | a calendar tool not in `--tools` |
| reading, writing and editing files in `workspace/files/` | a schedule (each one is a standing instruction) | a write in a scheduled run that you didn't always-allow |
| memory notes in `workspace/memory/` | a write the server marks destructive (if you added one) | the shell, web, browser: not offered at all |
| `notify_owner` (a message to you) | | |

File and memory writes don't ask because they stay inside `~/.wraithd/workspace/`, which the file tools can't leave
([files](files.md)). A memory write from a run that read someone else's text (a calendar invite) waits in the review
queue instead ([review queue](review-queue.md)).

After the run has read third-party text (any calendar result, any MCP tool description), a write that names people
(`attendees`) shows on the card which addresses you never wrote yourself ([security](security.md)).

## 5. What leaves the machine

With the settings above, wraithd itself opens exactly two kinds of connection: the model provider
(`api.openai.com`) and Telegram (`api.telegram.org`). The calendar server, a separate process, talks to Google
(`www.googleapis.com`, `oauth2.googleapis.com`), and `npx` to `registry.npmjs.org` when the package isn't cached.

wraithd doesn't police other processes' connections. To enforce that list at the OS level, a per-app firewall can:
[LuLu](https://objective-see.org/products/lulu.html) (free) or Little Snitch, with rules for the `wraithd` user's
`python` and `node`. That's optional and not part of wraithd.

The docker sandbox ([sandbox](sandbox.md)) doesn't help here: it runs the shell (off) and MCP servers you mark
`--sandbox`, never wraithd itself, and it mounts nothing, so the calendar server couldn't read its credentials or
token inside it.

## 6. Check it

```sh
wraithd audit                     # every rule pass or FAIL with a fix; exit 1 on a FAIL
wraithd doctor                    # the whole setup, one line per check
wraithd mcp tools calendar        # each tool: read-only, asks, not offered
wraithd runs list                 # what ran: from Telegram, from a schedule
wraithd runs show <id>            # one run: every tool call, every approval
wraithd events                    # between runs: strangers who wrote to the bot, tools that changed
wraithd schedules list            # what runs on its own
```

`audit` after lockdown, with the calendar added:

```
  ok    /Users/wraithd/.wraithd is yours alone (mode 700)
  ok    Nothing in it is writable by other accounts
  ok    Everything in it belongs to you
  ok    The API binds to 127.0.0.1 only
  ok    API token is set and private (mode 600)
  ok    Only the daemon's own origin may call the API
  ok    Shell: off
  ok    No keys in config.toml (they belong in the keychain)
  info  Web: search off, page reading off, browser off
  info  MCP calendar: only list-calendars, list-events, get-event, search-events, get-freebusy, get-current-time, create-event
  ok    The store (review queue, pins, event log) is readable by you alone
  ok    Sandbox: off (the shell, when on, runs as you after each "y")

All good.
```

## What is NOT protected

Be clear on these; none of the settings above change them.

- **The model provider sees the conversation**, including every calendar entry the agent reads. **Telegram** sees
  every message to and from the bot. Use a provider and a chat app you're willing to trust with that.
- **Your Telegram account is the key.** Whoever can write from your paired chat controls the agent. Turn on
  Telegram's two-step verification (Settings → Privacy and Security → Two-Step Verification), and lock your phone.
- **Calendar text is someone else's text.** An invite can carry instructions. The model may follow them in its
  answer; the rules in code stop it from acting on them without you (web is off, writes ask, unknown addresses are
  named on the card), but read the card before you tap **Allow**.
- **The calendar server is third-party code** with your Google token and the internet. wraithd limits which of its
  tools the model can call, not what the process does. Pinning the version and the separate user limit the damage.
- **The Google token is a plain file** in the `wraithd` user's home. Anyone who can act as that user (or as an
  administrator) can use it. Revoke it at myaccount.google.com → Security → Third-party access when you stop.
- **In Google's "Testing" mode the login lasts 7 days**, then the calendar stops working until you log in again
  ([first launch](first-launch.md#google-calendar)).
- **Files and memory inside the workspace are written without asking** (they can't leave it).
- **Administrators and root** on the Mac can read everything, the `wraithd` user's too. So can a macOS or kernel bug.
- **The web UI link** (`http://127.0.0.1:7878/#token=...`) is full control of the agent. Don't paste it anywhere.
- **Without the separate user**, all of the fences above are wraithd's own code, running with your permissions.

## Tests

`tests/test_first_launch.py` loads this page's config block as written, adds a stub of the calendar server with the
`mcp add` line above, checks that `audit` passes and what `mcp tools` shows, and runs the three starter messages over
a fake Telegram: no web, shell, browser or hidden calendar tool is offered, creating an event waits for the tap, and
a scheduled run can read but its write is refused.
