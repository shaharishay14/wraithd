# First launch: wraithd on your Mac, your calendar, Telegram

**For:** the owner, starting wraithd for the first time on their own Mac, with one Google Calendar connected and
Telegram as the way to talk to it, set up so it can't reach what it doesn't need ([lockdown](lockdown.md)).

## One-liner

> Make a separate macOS user, install wraithd with uv, run `onboard` for the model key and your Telegram bot (keys go
> in the keychain, the bot answers only your chat), paste the lockdown settings, log in to Google Calendar once and
> add it with only the tools you want, then message the bot. `doctor` and `audit` check it; `daemon stop` and the
> last section undo it.

Commands marked **(your account)** need your real Telegram, Google or OpenAI account and weren't run while writing
this. Everything else was run against a throwaway state folder, or by `tests/test_first_launch.py` ([how this guide
was tested](#how-this-guide-was-tested)).

## What you need

| Item | Cost | Notes |
|---|---|---|
| A Mac | | macOS with an admin account to make the second user. Python comes with uv. |
| An OpenAI API key | pay as you go | Make one just for wraithd at platform.openai.com → API keys, and set a monthly limit (Limits). An Anthropic key works too: pick `anthropic` in onboard. |
| Telegram and a bot | free | The bot is made with @BotFather in two minutes (step 3). |
| A Google account and a Google Cloud project | free | For the calendar's login (step 5). No billing account is needed for the Calendar API. |
| Node.js | free | The calendar server runs with `npx`. nodejs.org's installer or `brew install node`. |
| uv | free | Installs wraithd in its own environment (step 2). |

What the calendar server is: [`@cocal/google-calendar-mcp`](https://github.com/nspady/google-calendar-mcp), the most
used open-source Google Calendar MCP server (MIT). It can list, search, create, update and delete events and answer
invites across your calendars; you'll offer it only the reads and "create an event". Its Google login is an OAuth
token in a plain file, `~/.config/google-calendar-mcp/tokens.json`, readable by the macOS user it belongs to.

## 1. A user of its own

Do [lockdown step 1](lockdown.md#1-a-separate-macos-user-the-strongest-layer) first: a Standard macOS user called
`wraithd`, your home folder closed to it, then log in as `wraithd`. **Every step below runs in that account**, in its
Terminal. (In your own account it works the same, minus the fence: [what that means](lockdown.md#what-is-not-protected).)

## 2. Install

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh                 # uv, in ~/.local/bin   (your account: not run here)
exec $SHELL -l                                                   # so uv is on PATH
uv tool install git+https://github.com/shaharishay14/wraithd
wraithd --version                                                # wraithd 0.0.1
node --version && which npx                                      # the calendar server needs these
```

wraithd isn't on PyPI yet, so it installs from the repo. That build has no web UI (the daemon shows a placeholder,
and `doctor` reports `Web UI: not in this install`). Telegram is all you need. For the web UI (runs, approvals,
memory in a browser), build the release instead, which needs node:

```sh
git clone https://github.com/shaharishay14/wraithd && cd wraithd
scripts/release.sh                                               # the UI, then dist/wraithd-<version>-py3-none-any.whl
uv tool install --force dist/wraithd-*.whl                       # (not run here: CI runs this script)
```

## 3. The Telegram bot (your account)

In Telegram, open **@BotFather**:

1. `/newbot`, a name, then a username ending in `bot`. BotFather answers with the token (`123456:ABC...`). It's
   the bot's password: don't paste it anywhere but onboard.
2. `/setjoingroups` → your bot → **Disable**, so nobody can add it to a group.

Also turn on Telegram's two-step verification (Settings → Privacy and Security): your Telegram account is what
controls the agent.

## 4. Onboard

```sh
wraithd onboard
```

It asks, in order ([install](install.md)); Enter keeps what's in brackets:

- **Provider and models:** `openai`, `gpt-4o-mini` for every run, `gpt-4o` as the strong one.
- **The key:** pasted without echo, saved in the `wraithd` user's login keychain. Say yes to the check (one request
  for the model list). If macOS asks whether `python` may use the keychain, choose **Always Allow**.
- **Web search:** nothing to ask; lockdown turns it off in step 5.
- **Telegram:** yes, then the bot token. It's checked with Telegram (`getMe`) and saved in the keychain.
- **Background service:** yes. A launchd agent (`~/Library/LaunchAgents/com.wraithd.daemon.plist`) that starts at
  login and restarts after a crash. Its PATH is this shell's, so run onboard where `which npx` works.
- **Pairing:** it shows `/pair 123456`. Send that to your bot from your phone. That chat becomes the owner's: the bot
  answers it and nobody else, and a stranger who finds the bot gets no reply at all ([telegram](telegram.md)).

Keys never go in `config.toml`. To change one later: `wraithd key set openai` (or `telegram`). To pair again (a new
phone): `wraithd telegram pair`, and send the code it prints.

Without the daemon running (no background service), pair from onboard, then start it with `wraithd daemon` in a
terminal.

## 5. Lock it down

Open `~/.wraithd/config.toml`, replace its contents with [the lockdown block](lockdown.md#2-the-settings-one-block)
(keep your timezone and model lines), then:

```sh
wraithd daemon restart                                           # (launchd: not run here; tests use a fake)
wraithd audit                                                    # All good.
```

That turns off web search, opening pages, the shell, the browser, voice notes and the heartbeat, and lowers the
budgets. [Lockdown](lockdown.md) says why for each line.

## 6. Google Calendar

### Google's side (your account)

At [console.cloud.google.com](https://console.cloud.google.com), signed in as the Google account whose calendar
you want:

1. Create a project, e.g. `wraithd`.
2. **APIs & Services → Library → Google Calendar API → Enable.**
3. **Google Auth Platform** (the OAuth consent screen): app name `wraithd`, audience **External**, and under
   **Audience → Test users** add your own Gmail address. Leave it in **Testing**.
4. **Clients → Create client → Desktop app.** Download the JSON.
5. Move it into place, readable by this user only:

   ```sh
   mkdir -p ~/.config/google-calendar-mcp
   mv ~/Downloads/client_secret_*.json ~/.config/google-calendar-mcp/gcp-oauth.keys.json
   chmod 600 ~/.config/google-calendar-mcp/gcp-oauth.keys.json
   ```

### Log in once (your account)

```sh
GOOGLE_OAUTH_CREDENTIALS=~/.config/google-calendar-mcp/gcp-oauth.keys.json \
  npx -y @cocal/google-calendar-mcp@2.8.1 auth
```

A browser opens. Pick your account. Google warns the app isn't verified (it's your own): **Continue**, then allow
calendar access. The token lands in `~/.config/google-calendar-mcp/tokens.json`.

**The 7-day limit.** While the Google project is in Testing, the login expires after 7 days and the calendar tools
start failing ("could not run"). Run the `auth` line again, then `wraithd daemon restart`. To stop that, publish the
app (Google Auth Platform → Audience → **Publish app**): it stays unverified and only you use it, but the login lasts.

### Add it to wraithd

```sh
TOOLS=list-calendars,list-events,get-event,search-events,get-freebusy,get-current-time,create-event
wraithd mcp add calendar --provides calendar --tools $TOOLS \
  --env GOOGLE_OAUTH_CREDENTIALS=$HOME/.config/google-calendar-mcp/gcp-oauth.keys.json \
  -- npx -y @cocal/google-calendar-mcp@2.8.1 start --enable-tools $TOOLS
wraithd mcp tools calendar
wraithd daemon restart                                           # so the daemon connects it
```

`--enable-tools` is the server's own list and `--tools` is wraithd's; both say the same, so a tool missing from one
is still kept out by the other ([lockdown](lockdown.md#3-the-calendar-server-only-the-tools-you-name)). The path
after `--env` is stored in the keychain; the config names only the variable. `mcp tools` should show:

```
list-calendars               read-only       List all available calendars.
list-events                  read-only       List events from one or more calendars.
get-event                    read-only       Get one event by its id.
search-events                read-only       Search events by text.
get-freebusy                 read-only       Free/busy for calendars.
get-current-time             read-only       The current time in the calendar's timezone.
create-event                 asks            Create a new calendar event.
manage-accounts              not offered     (not in this server's tools list)
```

(The descriptions are the server's own; these are the test stub's.) The first connect records each tool as it is
now. If an upgrade later changes one, it asks again until you look at it (`wraithd review list`). Don't run
`wraithd mcp allow calendar create-event`: then every new event asks you first, and a scheduled run can only read.

## 7. Talk to it

Message your bot from your phone:

1. **"What's on my calendar tomorrow?"** It calls `list-events` and answers. Reads don't ask.
2. **"Add lunch with Sam on Wednesday at 1pm."** A card comes up: `Allow CALENDAR_CREATE_EVENT?` with the summary,
   start and end, and **Allow** / **Deny**. The card also says the run read third-party content first (the
   calendar server's tool descriptions): that's on every calendar card, and it's why invitees you never wrote
   yourself are named on it. Tap **Allow**; the event exists only after the tap. No tap within 60 seconds counts as
   **Deny**.
3. **"Every weekday at 8, send me today's calendar in three lines."** A card for the schedule (`schedule_create`):
   a standing instruction, so it asks once. From then on, at 8:00 in your `[owner] timezone`, it runs on its own and
   the answer arrives as `⏰ Send me today's calendar...`. On its own it can read the calendar but not change it: a
   write with nobody there to tap is refused. `wraithd schedules list` shows it, `wraithd schedules rm <id>` stops it.

## 8. Check it

```sh
wraithd doctor                    # every check one line; a fix under each failure
wraithd audit                     # the security rules; exit 1 on a FAIL
wraithd daemon status             # running, and the web UI link   (launchd: not run here)
wraithd daemon logs -n 50
wraithd runs list                 # the latest runs: from Telegram, from the schedule
wraithd runs show <id>            # one run: every tool call and approval
wraithd events                    # between runs, e.g. a stranger who wrote to the bot
```

On the git install, `doctor` reports `FAIL Web UI: not in this install`: expected (step 2). A calendar that didn't
connect shows in `daemon logs` and on the next run's `runs show` as an MCP error; the usual cause is the 7-day login.

## 9. Stop it, or remove everything

**Stop for now:** `wraithd daemon stop`. It starts again at the next login of the `wraithd` user. A schedule only
runs while the daemon does.

**Remove everything**, in this order, in the `wraithd` account:

```sh
wraithd schedules list                    # then: wraithd schedules rm <id> for each
wraithd mcp remove calendar               # the server, its keychain entry and its pins
wraithd daemon uninstall                  # unloads and deletes the launchd agent   (launchd: not run here)
wraithd key rm telegram
wraithd key rm openai
uv tool uninstall wraithd
rm -rf ~/.wraithd                         # settings, memory, the event log, workspace files
rm -rf ~/.config/google-calendar-mcp      # the Google token and the OAuth client file
```

Then, outside the Mac **(your account)**:

- Google: myaccount.google.com → Security → Third-party apps & services → `wraithd` → remove access; and delete the
  Cloud project.
- Telegram: @BotFather → `/deletebot` (or `/revoke` to only kill the token).
- OpenAI: platform.openai.com → API keys → delete the key.
- The macOS user: System Settings → Users & Groups → `wraithd` → Delete User → **Delete the home folder**. That alone
  removes everything above that lived in its home, its keychain included. If you closed your home folder in
  [lockdown](lockdown.md), `chmod 750 ~` puts it back.

## Later: a server

Running it on an always-on Linux box instead (a VPS or a mini PC) works the same way, with three differences: the
keys come from a `chmod 600` env file instead of a keychain ([keys](keys.md)), the service is a systemd user unit
that needs `loginctl enable-linger`, and the calendar login has to happen on a machine with a browser and the token
copied over. Telegram needs no open port either way. That guide isn't written yet.

## How this guide was tested

- **Against a throwaway state folder** (`WRAITHD_HOME` and `HOME` in a temp folder, a file keyring instead of the
  macOS Keychain, a fake OpenAI key, a fake bot token): `uv tool install` of this repo's checkout into a temp tool
  folder, `wraithd --version`, `onboard` (answers piped in; Telegram and the background service answered no), `key
  set telegram`, `telegram pair`, the lockdown block, `audit` (All good), `doctor`, `mcp add calendar` with the line
  above but a local stub server in place of `npx`, `mcp list`, `mcp tools calendar` (the output above), `mcp allow`
  on a hidden tool (refused), the daemon in the foreground on another port (`/api/health` answered), `runs list`,
  `schedules list`, `review list`, `events`, `daemon logs`, `key rm`.
- **By `tests/test_first_launch.py`** (`uv run pytest tests/test_first_launch.py`): the lockdown block read from
  [lockdown.md](lockdown.md), the stub calendar server (`tests/fixtures/fake_calendar_mcp.py`: the real server's tool
  names, annotations and `--enable-tools`), the pairing and the three messages above over a fake Telegram with a
  scripted model, the scheduled run refusing a write, a stranger ignored, and the removal commands.
- **Not run:** anything marked **(your account)**, the real `npx @cocal/google-calendar-mcp` server, `launchctl`
  (`daemon install | restart | status | stop | uninstall`; `tests/daemon/test_service.py` covers them with a fake),
  `scripts/release.sh`, and the uv installer.
