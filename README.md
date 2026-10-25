# Local Agent

A personal agent that runs on your own machine. Install one package, bring a model, and talk to it from
the terminal, a local web page or Telegram.

The point is the harness: the loop, security, memory, skills and progressive disclosure, built so you
can see and test every decision. Hard rules (the permission gate, taint tracking, the egress guard,
budgets) live in code, not in the prompt. State stays on your machine in `~/.local-agent/`.

## Status

Early. You can chat with it in the terminal: it remembers what you tell it (as markdown files you can edit),
follows a starter pack of skills, asks before it changes anything, and logs every step. Built-in tools (files,
web, schedules), apps over MCP, the background daemon and Telegram come next. The design notes are in
[docs/](docs/README.md).

## Try it

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run local-agent key set openai     # stored in the OS keychain
uv run local-agent chat
uv run local-agent runs list          # what each run did
uv run local-agent runs show <id>
```

Settings are in `~/.local-agent/config.toml`: the provider (`openai`, `anthropic`, `ollama` or any
OpenAI-compatible endpoint), the model, a stronger model for subtle skills, and budgets. For Anthropic models,
install the extra: `uv sync --extra anthropic`. Your standing orders go in `~/.local-agent/workspace/AGENTS.md`.

## Development

```sh
uv run pytest        # no test calls a model, needs a key or touches ~/.local-agent
uv run ruff check
```

## License

MIT
