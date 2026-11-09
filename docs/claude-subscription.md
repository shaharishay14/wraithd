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
3. Onboard shows the steps, then opens the real Claude Code right there in your terminal (press Enter):
   type /login → pick your Claude account → open the link → sign in → copy the code → paste it here → /exit.
   Claude Code gets the terminal itself (no pipes): wraithd never reads the link or the code. It runs the `claude`
   on your PATH, or the copy that came with the extra. Already signed in? Type `check` (or pick it). Without a
   terminal (a pipe, a script), onboard prints the steps for you to run `claude` in another terminal.
4. When Claude Code exits, onboard checks that it is on your login and writes `[engine] name = "claude-code"`.
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
memory and compaction, so you can switch engines mid-conversation.

Short chat messages ("What's 12 times 12", "skip D1") go to `quick_model` (Haiku), which starts answering sooner;
everything else (a long message, an attachment, a skill named, a `/command`) stays on `chat_model` (Sonnet). Start a
message with `think` or `/sonnet` to put it on Sonnet anyway; the word is left out of what the model sees. The
rules are in [engines.md](engines.md#a-chat-messages-model).

Chat keeps one warm Claude Code session per conversation and model (two with `quick_model` on): only the first
message pays the ~4.6 seconds it takes Claude Code to start, and later messages send only the new text. A session nobody has used for `idle_minutes`
(10 by default) is closed; the next message starts a fresh one and hands it the conversation so far. Outside text
read in an earlier message still taints the next one, as on the API engine. When you quit chat (`/quit` or
Ctrl-D), its sessions close before wraithd exits. `wraithd runs show` lists each start as an `engine_connect`
event with its milliseconds, apart from the run's own `duration_ms`.

Your own Claude chats and wraithd share one plan's limits, so wraithd picks the model by the job:

```toml
[engine]
name = "claude-code"
chat_model = "sonnet"        # chats from the terminal, the web UI and Telegram
quick_model = "haiku"        # short chat messages ("": every chat on chat_model)
background_model = "haiku"   # the heartbeat, schedules, memory distill and compaction
schedule_model = ""          # scheduled runs ("": background_model)
fallback = false             # true: use the API key or Ollama when the plan limit hits (see Plan limits)
idle_minutes = 10            # close a warm chat session after this long unused
```

A scheduled run whose message names a `model: strong` skill (say `run news-feed`) runs on `chat_model` instead,
since Claude Code can't switch models mid-run (see [engines.md](engines.md#a-scheduled-runs-model)). Cost note: a
Sonnet scheduled run uses roughly 3x the plan of a Haiku one, so mark only the skills that need it, and set
`schedule_model` only if every schedule should pay that.

Onboard writes only `name`; the others keep these defaults until you change them. wraithd always names the model:
left to itself, Claude Code picks Opus, the priciest.

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

## The rules wraithd keeps

1. It never reads `~/.claude/.credentials.json` or Claude Code's keychain entry.
2. It never sets `CLAUDE_CODE_OAUTH_TOKEN`, and never asks you to paste a token.
3. It never calls Anthropic's API directly with a login token. Only the unmodified Claude Code program, started by
   claude-agent-sdk, talks to Anthropic.
4. Claude Code gets no built-in tools (`tools=[]`, `strict_mcp_config=True`, `setting_sources=[]`). It sees only
   wraithd's tools, after the gate wraps them.
5. It falls back to another engine only if no write completed and nothing streamed.
6. It refuses subscription mode if `CLAUDE_CODE_USE_BEDROCK` or `CLAUDE_CODE_USE_VERTEX` is set (Claude Code reports
   no key there too, but it isn't your login).
7. API keys and Ollama keep working: the subscription is one option, never the only one.
8. Its tests fake the Claude side and never touch the real login or your home folder.

What stays on your disk: no conversation transcripts or project files are left behind (nothing under
`~/.claude/projects`; Claude Code runs with `--no-session-persistence`). Claude Code's own bookkeeping is expected:
`~/.claude.json`, its plugin cache (`~/.claude/plugins/`) and temporary session files (the `sessions` and `backups`
folders' timestamps change).

Doctor starts Claude Code with no tools, reads which credential it chose (`apiKeySource` in its first message),
and, only if that's your login, sends one short message on the cheap background model: Claude Code reports no key
when nobody is signed in too, so only the reply shows the login works. Audit only builds the options and never
starts Claude Code. A test checks that they never open Claude Code's credentials file, never
read its keychain entry and never set `CLAUDE_CODE_OAUTH_TOKEN`.

## The checks

`wraithd doctor`, with `[engine] name = "claude-code"`:

| Line | Means |
|---|---|
| `ok   Claude subscription login in use` | Claude Code chose your login and answered one short message on the background model |
| `FAIL Claude Code is using ANTHROPIC_API_KEY, not your login` | a key won: unset it, then `claude` and `/login` |
| `FAIL Claude Code is using an apiKeyHelper` | remove `apiKeyHelper` from your Claude Code settings |
| `FAIL Claude Code is signed in with an Anthropic Console account` | that bills the API: `/logout`, then `/login` with your Claude account |
| `FAIL Claude Code isn't signed in` | run `claude` and type `/login` |
| `warn ... signed in, but your plan limit is reached right now` | the login works; wait for the limit to reset |
| `FAIL CLAUDE_CODE_USE_BEDROCK is set` (or `_VERTEX`) | unset it where wraithd runs |
| `FAIL Claude Code extra not installed` | `uv tool install 'wraithd[claude]'` |

`wraithd audit`, with claude-code on:

- Claude Code's options keep its own tools off (`tools=[]`), use only wraithd's MCP server
  (`strict_mcp_config`), and load none of your Claude Code settings (`setting_sources=[]`).
- A FAIL when `CLAUDE_CODE_USE_BEDROCK` or `CLAUDE_CODE_USE_VERTEX` is set.
- A warning when `ANTHROPIC_API_KEY` is set in the environment: elsewhere it would quietly win over the login.
- A FAIL if wraithd's config or wraithd's own keychain entries hold a Claude login token (`sk-ant-oat...`).
  It looks only at wraithd's own entries, never at Claude Code's.
- With `[web] search = "claude"` (or claude-code on): the web search helper's options allow exactly `WebSearch`,
  no MCP servers, none of your settings, no token or key (docs/web.md). It's the only Claude Code wraithd starts
  with one of Claude Code's own tools.

## Plan limits

wraithd and your own Claude chats share one plan. When the plan's limit is hit:

- With `[engine] fallback = true` and an API key or Ollama set up, a run falls back to it, but only if nothing has
  happened yet: no write ran and no text was shown. Otherwise the run stops and says so, so nothing is done twice.
- Without a fallback, you get one plain line: the plan limit is reached, and it resets later.

The daily token budget is on by default in subscription mode (2,000,000 weighted tokens a day, unless you set
`[budget] daily_tokens` yourself). Cache reads count a tenth ([tokens](tokens.md)), and `wraithd runs list` shows
raw and weighted tokens per run. It is wraithd's own limit, not the plan's, and its message says so and how to raise
it.

## Turns

A Claude Code run makes at most `[engine] max_turns` model calls (default 8, the API engine's limit too). When it
uses them all, Claude Code ends with `error_max_turns`: wraithd logs a `stop` (`max_turns`) and answers plainly that
the step limit was hit, listing the writes already done (or that nothing was changed), so the owner doesn't repeat
them. The run's outcome is `answered`, as on the API engine; a warm session stays open. A skill that needs more
turns (a full receipt-tracker log is about 8 or 9) needs a higher `max_turns`.

## Switching back to a key or a local model

Run `wraithd onboard` again and answer `api-key` (then pick openai or anthropic and give the key) or `local`
(Ollama). Or edit the config:

```toml
[engine]
name = "agents-sdk"   # the default: [model] provider and its key, or Ollama
```

Your history, memory and schedules stay as they are: wraithd keeps them, not Claude Code. To sign Claude Code
out, run `claude` and type `/logout`.
