# Keys

**Files:** `config.py` (`get_key`, `set_key`), `mcp/config.py` (`get_secrets`, `env_name`), `core/secrets.py`

## One-liner

> Keys live in the OS keychain when there is one, and come from environment variables when there isn't. Whatever
> sets the environment is up to you: an env file, systemd, or a password manager. Nothing here needs a paid service.

## Where a key is looked up

1. The keychain (`wraithd key set <name>`): the macOS Keychain, or the Secret Service on a Linux desktop.
2. Else its environment variable:

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

- Every one of these variables, plus `OP_SERVICE_ACCOUNT_TOKEN`, is removed from the environment of a
  `shell_run` command, and their values are redacted from anything the model, the event log or memory sees.
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
