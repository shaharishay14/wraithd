# Keys

**Files:** `config.py` (`get_key`, `set_key`), `mcp/config.py` (`get_secrets`, `env_name`), `core/secrets.py`

## One-liner

> Keys live in the OS keychain when there is one, and come from environment variables when there isn't. Whatever
> sets the environment is up to you: an env file, systemd, or a password manager. On a Linux server, systemd's
> encrypted credentials keep them encrypted on disk. Nothing here needs a paid service.

## Where a key is looked up

1. systemd's credentials folder, when wraithd runs as a service with credentials (`$CREDENTIALS_DIRECTORY` is set):
   a file named like the key's variable below (`OPENAI_API_KEY`), or like its wraithd name (`openai`). See
   [the recipe](#recipe-systemd-encrypted-credentials-ubuntu-2404).
2. The keychain (`wraithd key set <name>`): the macOS Keychain, or the Secret Service on a Linux desktop.
3. Else its environment variable:

| Key | Variable |
|---|---|
| openai | `OPENAI_API_KEY` |
| anthropic | `ANTHROPIC_API_KEY` |
| serpapi | `SERPAPI_KEY` |
| telegram | `TELEGRAM_BOT_TOKEN` |
| elevenlabs | `ELEVENLABS_API_KEY` |
| an MCP server's value | `WRAITHD_MCP_<SERVER>_<NAME>`, e.g. `WRAITHD_MCP_X_X_TOKEN` for `--env X_TOKEN` on server `x` |

A headless Linux server has no keychain service. There `key set`, `onboard` and `mcp add NAME=VALUE` say which
variable to set instead, and `mcp add --env NAME` doesn't ask for a value that's already in the environment.
`wraithd doctor` shows a key as set wherever it came from.

## Keeping keys away from the agent

- Every one of these variables, and every 1Password variable (`OP_*`: `OP_SERVICE_ACCOUNT_TOKEN`, `OP_SESSION_...`,
  `OP_CONNECT_TOKEN`), is kept from every process wraithd starts: removed from a `shell_run` command's environment,
  blanked in Claude Code's (claude-agent-sdk passes it wraithd's whole environment, so wraithd sets each one to
  empty there), and never given to an MCP stdio server, which starts with only `PATH`, `HOME`, `USER`, `LOGNAME`,
  `SHELL`, `TERM` and its own values. `wraithd audit` checks Claude Code's.
- The one exception is `op read` for an `op://` reference in an MCP server's settings ([MCP](mcp.md)): the 1Password
  CLI needs the token, so it gets wraithd's environment. Nothing it reads is passed on with the token.
- Every key's value, wherever it came from (a credential file, the keychain, a variable), is redacted from anything
  the model, the event log or memory sees.
- An MCP server gets only its own values, never the model keys or another server's.

## Recipe: an env file (free, the default)

```sh
install -m 600 /dev/null ~/.config/wraithd.env
cat >> ~/.config/wraithd.env <<'EOF'
OPENAI_API_KEY=sk-...
TELEGRAM_BOT_TOKEN=123456:ABC...
EOF
systemctl --user edit wraithd
```

In the editor, add:

```ini
[Service]
EnvironmentFile=%h/.config/wraithd.env
```

Then `systemctl --user restart wraithd`. The override is a drop-in, so `wraithd daemon install` doesn't
replace it. For a command in your shell: `set -a; . ~/.config/wraithd.env; set +a; wraithd doctor`.

The file is plain text that only your user can read: the same protection as a `.env` file. Use disk encryption
on the box.

## Recipe: 1Password (optional, your own subscription)

A service account reads keys from one vault, and `op run` hands them to wraithd as environment variables.
Service accounts come with a 1Password subscription, which you pay for. Check that Developer → Service accounts
is offered on your plan.

1. In 1Password, make a vault for wraithd. Service accounts can't read Personal, Private or the default
   Shared vault. Add an item per key, e.g. "OpenAI" with the key in its `credential` field.
2. Developer → Service accounts → new, with read access to that vault only. Copy the token (shown once).
3. On the server, install the 1Password CLI (2.18 or later), then put the token in a file only you can read:

   ```sh
   install -m 600 /dev/null ~/.config/wraithd-op.env
   echo 'OP_SERVICE_ACCOUNT_TOKEN=ops_...' >> ~/.config/wraithd-op.env
   ```

4. Write references, not keys, in `~/.config/wraithd-keys.env`:

   ```sh
   OPENAI_API_KEY=op://wraithd/OpenAI/credential
   TELEGRAM_BOT_TOKEN=op://wraithd/Telegram/credential
   ```

5. Try it: `set -a; . ~/.config/wraithd-op.env; set +a; op run --env-file ~/.config/wraithd-keys.env -- wraithd doctor`
6. The daemon: `systemctl --user cat wraithd` shows the `ExecStart` line. Then `systemctl --user edit wraithd`:

   ```ini
   [Service]
   EnvironmentFile=%h/.config/wraithd-op.env
   ExecStart=
   ExecStart=/usr/bin/op run --env-file=%h/.config/wraithd-keys.env -- <the ExecStart command shown above>
   ```

What it buys: the token on disk can only read that one vault, you can revoke it, keys rotate in one place, and
1Password logs each read. What it doesn't: anyone who can act as your user can still use the token. Service
accounts are rate-limited, so a daemon stuck in a restart loop can be blocked for a while.

Other managers (Bitwarden `bws run`, Doppler, Infisical, `pass`, sops) work the same way: anything that starts
the command with the variables set.

## Recipe: systemd encrypted credentials (Ubuntu 24.04)

systemd (255 on Ubuntu 24.04) can keep each key encrypted on disk and hand it, decrypted, to one service only:
in a folder under `/run/credentials/` that only that service's user can read, for as long as it runs. No
password manager, no token on disk, and no key in any environment variable, so nothing wraithd starts can
inherit it. This is for a **system** service (`/etc/systemd/system/wraithd.service` with `User=`): a user unit
can't decrypt with the machine's key.

1. Encrypt each key, typed at a hidden prompt (it never lands in your shell history). The file name is the
   variable's name from the table above:

   ```sh
   sudo install -d -m 700 /etc/credstore.encrypted
   read -rs KEY && printf '%s' "$KEY" | sudo systemd-creds encrypt --name=OPENAI_API_KEY - /etc/credstore.encrypted/OPENAI_API_KEY; unset KEY
   read -rs KEY && printf '%s' "$KEY" | sudo systemd-creds encrypt --name=TELEGRAM_BOT_TOKEN - /etc/credstore.encrypted/TELEGRAM_BOT_TOKEN; unset KEY
   # an MCP server's value: the WRAITHD_MCP_<SERVER>_<NAME> name
   read -rs KEY && printf '%s' "$KEY" | sudo systemd-creds encrypt --name=WRAITHD_MCP_X_X_TOKEN - /etc/credstore.encrypted/WRAITHD_MCP_X_X_TOKEN; unset KEY
   ```

   The `--name` must match the name in the unit below. Without a TPM (a cloud VM such as Lightsail), systemd
   encrypts with the host key in `/var/lib/systemd/credential.secret`, readable by root only: the files are
   useless on another machine or from a backup without that key. Check one with
   `sudo systemd-creds decrypt /etc/credstore.encrypted/OPENAI_API_KEY - | wc -c` (the length, not the key).

2. Give them to the service in a drop-in (`sudo systemctl edit wraithd`), one line per key:

   ```ini
   [Service]
   LoadCredentialEncrypted=OPENAI_API_KEY:/etc/credstore.encrypted/OPENAI_API_KEY
   LoadCredentialEncrypted=TELEGRAM_BOT_TOKEN:/etc/credstore.encrypted/TELEGRAM_BOT_TOKEN
   LoadCredentialEncrypted=WRAITHD_MCP_X_X_TOKEN:/etc/credstore.encrypted/WRAITHD_MCP_X_X_TOKEN
   UMask=0077
   ```

   The service itself, if you don't have one yet (`/etc/systemd/system/wraithd.service`; `wraithd daemon install`
   writes a user unit instead, which can't use these):

   ```ini
   [Unit]
   Description=wraithd: the API, web UI, scheduler and Telegram
   Wants=network-online.target
   After=network-online.target

   [Service]
   User=wraithd
   Environment=WRAITHD_HOME=/home/wraithd/.wraithd PYTHONUNBUFFERED=1
   ExecStart=/home/wraithd/wraithd/.venv/bin/python -m wraithd daemon
   UMask=0077
   Restart=on-failure
   RestartSec=10

   [Install]
   WantedBy=multi-user.target
   ```

   Replace `wraithd` in `User=` and the paths with the account and checkout it runs from. If the unit's
   `ExecStart` starts with `op run`, reset it in the drop-in (`ExecStart=` on its own line, then the plain
   command) and drop the `EnvironmentFile=` with the 1Password token: the keys no longer need it.

3. `sudo systemctl daemon-reload && sudo systemctl restart wraithd`, then
   `sudo systemctl show wraithd -p LoadCredentialEncrypted` lists them, and `wraithd daemon logs` shows the
   daemon starting with them (Telegram polling, the model answering). `wraithd doctor` in your own shell doesn't
   see them: the folder exists only for the service. A shell command you approve runs as the service's user and
   could read the folder by path: the approval on every command is what stops that, as it is for an env file.

What it buys over an env file: the keys are encrypted at rest, only this service gets them, and they're in no
environment at all. What it doesn't: root, and anyone who can act as the service's user while it runs, can still
read them.

