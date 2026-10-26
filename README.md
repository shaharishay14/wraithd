# Local Agent

A personal agent that runs on your own machine. Install one package, bring a model, and talk to it from
the terminal, a local web page or Telegram.

The point is the harness: the loop, security, memory, skills and progressive disclosure, built so you
can see and test every decision. Hard rules (the permission gate, taint tracking, the egress guard,
budgets) live in code, not in the prompt. State stays on your machine in `~/.local-agent/`.

## What it is

An agent you own, the way you own your notes. It runs as one process on your Mac or Linux machine. Its state
is plain files plus one SQLite file in `~/.local-agent/`, and the only thing that leaves the machine is what you
send to your model provider (or nothing, with a local model through Ollama).

What it does with nothing but a model:

- **Talks** in the terminal (`local-agent chat`), on a local web page, and in your own Telegram bot, which
  answers only you.
- **Remembers** what you tell it, as markdown notes you can open and edit, and keeps a short profile of you
  in every conversation.
- **Follows skills**: a starter pack (morning brief, inbox triage, research report, meeting prep, weekly
  review) that loads only when a request matches, plus rules you add ("never include newsletters").
- **Works**: reads web pages (and searches, with a key), reads and writes files in its own folder, keeps
  schedules and reminders that fire with the terminal closed, and runs shell commands only if you turn that on
  and say yes to each one.
- **Connects your apps** as MCP servers (`local-agent mcp add`). Reading runs freely; anything else asks
  first, until you mark it "always allow".

It's the indie take on OpenClaw: one process, one owner, a few channels, no marketplace. Every step of every run is an event you can inspect (`local-agent runs show <id>`). The design notes
are in [docs/](docs/README.md), one per mechanism.

## Install

Requires macOS or Linux, Python 3.12+ and [uv](https://docs.astral.sh/uv/) (or pipx).

```sh
uv tool install local-agent           # or: pipx install local-agent
local-agent onboard                   # model and key, search, Telegram, the background service
local-agent doctor                    # checks everything, with a fix for anything wrong
local-agent audit                     # checks the setup against the security rules
local-agent daemon status             # also: start, stop, restart, logs, uninstall
local-agent update                    # upgrade and restart the service
```

## From a checkout

```sh
uv sync
uv run local-agent key set openai     # stored in the OS keychain
uv run local-agent chat
uv run local-agent runs list          # what each run did
uv run local-agent runs show <id>
uv run local-agent events             # what happened between runs: distill, compaction, ignored chats
uv run local-agent schedules list
uv run local-agent key set serpapi    # optional: web search
uv run local-agent mcp add gmail --provides email --env GMAIL_TOKEN -- <command that starts the server>
uv run local-agent key set telegram   # optional: your bot's token
uv run local-agent daemon             # prints http://127.0.0.1:7878/#token=...
uv run local-agent telegram pair      # then send /pair <code> to your bot
```

Settings are in `~/.local-agent/config.toml`: the provider (`openai`, `anthropic`, `ollama` or any
OpenAI-compatible endpoint), the model, a stronger model for subtle skills, budgets, your timezone, and the
shell switch (off by default). Your standing orders go in `~/.local-agent/workspace/AGENTS.md`; the agent's
files are in `~/.local-agent/workspace/files/`.

## How it stays safe

Prompts can be talked out of anything, so the rules that must hold are in code
([Security](docs/security.md), [From EC2 to laptop](docs/ec2-to-laptop.md)):

- **The permission gate.** Every tool call passes it. Reads run; writes ask you (a y/N in the terminal, a card
  in the web UI, buttons in Telegram); unknown tools never run. Unattended runs (schedules) only do writes
  you pre-approved, and never run the shell or a tool its server marks destructive.
- **Taint tracking.** Once a run reads someone else's text (an email, a page, an MCP tool's output or
  description), a pre-approved write may only go to recipients you wrote yourself. An email that says "forward
  everything to eve@" gets the agent nowhere.
- **The egress guard.** Pages the agent opens must resolve to public addresses on every hop, so a link in an
  email can't reach the agent's own API, your router or anything else on your network.
- **Secrets never reach the model.** Keys live in the OS keychain, each MCP server gets only its own, and
  token-shaped strings are redacted from tool results, the event log and memory.
- **The local API** listens on 127.0.0.1 only, with a token and Origin and Host checks, so a website you visit
  can't drive it.
- **Telegram answers only you.** Other chats get no reply and no model call; they're logged at most once an
  hour.
- **Budgets.** Per-run limits on tokens and tool calls, an optional daily budget, and stop rules for broken
  tools and loops.
- **`local-agent audit`** checks the live setup against these rules.

## How it does

The same harness on gpt-4o, Claude and a local model: ten end-to-end tasks in a fake world (an inbox with a
prompt injection and a leaked key, a calendar, the web, a broken weather service), graded by checks on the event
log and by a judge model ([Evals](docs/evals.md)).

<!-- Paste eval-results/report.md here after the paid run (docs/evals.md, "The paid run"). -->

*The table goes here once the paid run is done.*

## Development

```sh
uv run pytest        # no test calls a model, needs a key or touches ~/.local-agent, the keychain or services
uv run ruff check
uv run python -m local_agent.evals --scripted   # the golden evals offline (docs/evals.md for the paid run)
scripts/release.sh   # the web UI (built on a copy of frontend/) into the wheel, in dist/
```

## License

MIT
