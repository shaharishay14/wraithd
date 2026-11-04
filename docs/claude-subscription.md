# Claude subscription

**Files:** `onboard.py` (the choice), `doctor.py` (the login check), `audit.py` (the rules), `engines/claude_code.py`

## One-liner

> If you pay for Claude Pro or Max, wraithd can think with your plan instead of an API key. It runs Anthropic's own,
> unmodified Claude Code program, you sign in to it yourself, and wraithd never sees the login. The gate,
> approvals, taint tracking and the event log work exactly as they do on an API key.

## Set it up

1. Install wraithd with the Claude extra: `uv tool install 'wraithd[claude]'` (or `pip install 'wraithd[claude]'`).
   The extra is the Claude Agent SDK, which carries the Claude Code program (about 300 MB, so it isn't in the
   plain install).
2. Run `wraithd onboard` and answer `subscription` to "How should wraithd think?".
3. When it asks, open another terminal, run `claude` and type `/login`. Sign in with your Claude account. On a
   server with no browser, Claude Code prints a link: open it on any device, then paste the code back.
4. Press Enter in onboard. It checks that Claude Code is on your login and writes `[engine] name = "claude-code"`.
   No key is asked for.
5. `wraithd doctor` says `Claude subscription login in use`, and `wraithd audit` passes.

Already set up? Add this to `~/.wraithd/config.toml` and run `wraithd doctor`:

```toml
[engine]
name = "claude-code"
```

## How it works

Claude Code replaces only the thinking loop. wraithd starts it with its own tools turned off and its usual
settings ignored, and hands it wraithd's guarded tools instead, so every action still goes through wraithd's gate:
a write still shows an approval card, and `wraithd runs show` lists every tool call. wraithd keeps the history,
memory and compaction; Claude Code gets them as one prompt per run. Session files aren't saved.

Your own Claude chats and wraithd share one plan's limits, so wraithd picks the model by the job:

```toml
[engine]
name = "claude-code"
chat_model = "sonnet"        # chats from the terminal, the web UI and Telegram
background_model = "haiku"   # the heartbeat, schedules, memory distill and compaction
fallback = false             # true: use the API key or Ollama when the plan limit hits (see Plan limits)
```

Onboard writes only `name`; the others keep these defaults until you change them.

## Who may use it

From Anthropic's Claude Code legal page: an end user may sign in to the unmodified Claude Code program with their
own subscription. Apps may not offer Claude.ai login themselves, route requests through someone's Free, Pro or Max
credentials, or collect, store or pass on those credentials. Pro and Max limits assume ordinary, individual use.

| Setup | Subscription mode? |
|---|---|
| Your own wraithd | Yes |
| Someone running wraithd on their own machine, signed in themselves | Yes |
| A company server where one login serves many employees | No: use the company's API key |
| Hosting wraithd for other people | Only with Anthropic's Commercial Terms, each user's own login, and Anthropic confirming the setup |

Anthropic tells products built on the Agent SDK to use API keys, and has changed the subscription rules several
times. Personal use on your own machine is the case this mode is for. If you plan to offer wraithd to others on
subscriptions, ask Anthropic first.

## What wraithd never does

1. Read `~/.claude/.credentials.json` or the keychain entry Claude Code uses.
2. Ask you to paste a Claude token, or set `CLAUDE_CODE_OAUTH_TOKEN` itself.
3. Call Anthropic's API directly with your login.
4. Modify, patch or wrap the Claude Code program.
5. Make the subscription the only way to run: API keys and Ollama stay supported.

The doctor and audit checks only start Claude Code and read which credential it chose (`apiKeySource` in its first
message). A test checks that they never open Claude Code's credentials file and never set `CLAUDE_CODE_OAUTH_TOKEN`.

## The checks

`wraithd doctor`, with `[engine] name = "claude-code"`:

| Line | Means |
|---|---|
| `ok   Claude subscription login in use` | Claude Code started and chose your login |
| `FAIL Claude Code is using ANTHROPIC_API_KEY, not your login` | a key won; unset it, or run `claude` and `/login` |
| `FAIL Claude Code is set to Bedrock or Vertex` | `CLAUDE_CODE_USE_BEDROCK` or `CLAUDE_CODE_USE_VERTEX` is set; unset it |
| `FAIL Claude Code is not installed` | install the extra: `uv tool install 'wraithd[claude]'` |

`wraithd audit`, with claude-code on:

- Claude Code's options keep its own tools off (`tools=[]`), use only wraithd's MCP server
  (`strict_mcp_config`), and load none of your Claude Code settings (`setting_sources=[]`).
- A warning when `ANTHROPIC_API_KEY` is set in the environment: elsewhere it would quietly win over the login.
- A FAIL if wraithd's config or wraithd's own keychain entries hold a Claude login token (`sk-ant-oat...`).
  It looks only at wraithd's own entries, never at Claude Code's.

## Plan limits

wraithd and your own Claude chats share one plan. When the plan's limit is hit:

- With `[engine] fallback = true` and an API key or Ollama set up, a run falls back to it, but only if nothing has
  happened yet: no write ran and no text was shown. Otherwise the run stops and says so, so nothing is done twice.
- Without a fallback, you get one plain line: the plan limit is reached, and it resets later.

The daily token budget is on in subscription mode, and `wraithd runs` shows the tokens per run.

## Switching back to a key or a local model

Run `wraithd onboard` again and answer `api-key` (then pick openai or anthropic and give the key) or `local`
(Ollama). Or edit the config:

```toml
[engine]
name = "agents-sdk"   # the default: [model] provider and its key, or Ollama
```

Your history, memory and schedules stay as they are: wraithd keeps them, not Claude Code. To sign Claude Code
out, run `claude` and type `/logout`.
