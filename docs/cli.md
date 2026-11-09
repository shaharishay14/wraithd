# The terminal

On a real terminal, `wraithd` picks instead of asking you to type: onboarding and approvals are pickers, lists are
tables, and the chat edits lines, keeps history and draws answers as Markdown while they stream. It's monochrome
like the site (your terminal's own text color, grey and dim) with violet only as the accent. It never sets a
background color, so it reads the same on a dark and a light terminal.

The code is in `src/wraithd/tui/` (theme, picker, chat view, tables, the icon) and
`src/wraithd/channels/terminal.py` (`RichTerminal`).

## Pickers

Every choice in `wraithd onboard` (how wraithd thinks, provider, model, every yes/no, the Telegram steps), the
approval card in the chat, and the commands below when you leave out the id.

On the Claude subscription, onboarding's "Sign in to Claude" opens the real Claude Code in this terminal (it takes the
terminal over until you type /exit; wraithd reads nothing of it), then checks the login (docs/claude-subscription.md).

| Key | Does |
|---|---|
| ↑ ↓ (Ctrl-P, Ctrl-N, Tab) | move |
| Enter | pick the highlighted one |
| 1-9 | pick that one, at once |
| letters | jump to the first option that starts with them: `y` Enter is yes, `d` Enter is Deny, `anthropic` Enter is anthropic |
| Backspace | undo a typed letter |
| Ctrl-D | stop (onboard: what's saved stays saved; an approval: denied) |
| Ctrl-C | stop, as everywhere |

A letter never picks on its own, so the rest of a word you type can't spill into the next question. The model
picker lists the known models and `other…`; you can also type any name and press Enter (when it matches none of
the listed ones, nothing is highlighted and Enter takes what you typed).

Approvals highlight **Deny**: Enter denies, `y` Enter (or `a` Enter) allows once.

### Commands that pick their id

When the id is left out, these offer a list instead of failing: `wraithd schedules rm`, `runs show`,
`runs tokens`, `key rm`, `review show|approve|reject`, `mcp remove`. Giving the id works as before.

## Chat

On the `claude-code` engine, chat starts Claude Code as soon as it opens, while you type the first message
(docs/engines.md, Warm sessions).

| Key | Does |
|---|---|
| Enter | send |
| Esc Enter | a new line in the same message |
| ↑ ↓ | earlier messages (this session) |
| Tab | complete a /command |
| Ctrl-D, `/quit` | end the chat (Ctrl-C too); warm Claude Code sessions close first, even one still starting |

| Command | Does |
|---|---|
| `/help` | the commands and keys |
| `/new` | end this conversation (it's remembered, as when you quit) and start a fresh one; on the Claude subscription its Claude Code starts right away, as when the chat opens (plain mode takes `/new` too) |
| `/runs` | the latest runs |
| `/schedules` | schedules and reminders |
| `/model` | what wraithd thinks with (change it with `wraithd onboard`) |
| `/clear` | clear the screen |
| `/quit` | end the chat |

A line at the top says what thinks (`Claude subscription · sonnet`, or `openai · gpt-4o-mini`) and the
workspace. Each tool call is one dim line with a spinner, then `✓ tool  allowed` or `approved by you`, or
`✗ tool  denied by you`; a tool that failed is `✗ tool  allowed · error`, and one a rule refused `✗ tool  denied`
(the gate's `tool_result` callback: ok, error or denied; plain mode ignores it). An approval is a box naming the tool and its arguments; a recipient that came from
third-party text, or third-party text read earlier in the run, is a red warning at the top of the box. After each
answer, a dim footer: tokens and time.

An answer of exactly `NO_REPLY` (the run already messaged you with `notify_owner`) prints nothing, in both looks:
text that could still become `NO_REPLY` waits until it can't, so it never flashes on screen. The rich look still
shows the tool lines and the footer. The conversation keeps no assistant message for it.

A message that starts with a path (`/etc/hosts what's in it?`) goes to the agent; a single unknown `/word` is
taken as a typo.

## Plain mode

Plain mode is the terminal as it always was, byte for byte: typed answers (`[y/N]`, `(a | b | c) [default]`), no
colors, no tables, no icon. It's on whenever:

- stdin or stdout isn't a terminal (pipes, scripts, cron, CI),
- `NO_COLOR` is set (to anything but empty),
- `WRAITHD_PLAIN=1`,
- or you run `wraithd --plain ...` (a global flag, before the command).

In plain mode `/help` and the other chat commands go to the agent like any message, as before, and a command
missing its id is an error, as before. `--json`, the daemon, Telegram and the web UI never use any of this.

Tests play the owner in plain mode (`tests/scripted.py`); `tests/cli/test_plain.py` holds transcripts recorded
from before the rich look and checks they're unchanged. The rich look is tested through prompt_toolkit's pipe input
(`tests/cli/`).
