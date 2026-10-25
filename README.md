# Local Agent

A personal agent that runs on your own machine. Install one package, bring a model, and talk to it from
the terminal, a local web page or Telegram.

The point is the harness: the loop, security, memory, skills and progressive disclosure, built so you
can see and test every decision. Hard rules (the permission gate, taint tracking, the egress guard,
budgets) live in code, not in the prompt. State stays on your machine in `~/.local-agent/`.

## Status

Early. You can chat with it in the terminal: it remembers what you tell it (as markdown files you can edit),
follows a starter pack of skills, reads web pages and (with a search key) searches, works with files in its own
folder, can run shell commands if you turn that on and say yes to each one, and keeps schedules and reminders.
Your apps connect as MCP servers (`local-agent mcp add`): reading runs freely, anything else asks until you
always-allow it. It asks before it changes anything outside its folder, and logs every step. `local-agent daemon`
runs it in the background of a terminal: a local API and web UI, schedules firing on time, and your own Telegram
bot. Installing it as a service comes next. The design notes are in [docs/](docs/README.md).

## Try it

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run local-agent key set openai     # stored in the OS keychain
uv run local-agent chat
uv run local-agent runs list          # what each run did
uv run local-agent runs show <id>
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

## Development

```sh
uv run pytest        # no test calls a model, needs a key or touches ~/.local-agent
uv run ruff check
```

## License

MIT
