# Local Agent

A personal agent that runs on your own machine. Install one package, bring a model, and talk to it from
the terminal, a local web page or Telegram.

The point is the harness: the loop, security, memory, skills and progressive disclosure, built so you
can see and test every decision. Hard rules (the permission gate, taint tracking, the egress guard,
budgets) live in code, not in the prompt. State stays on your machine in `~/.local-agent/`.

## Status

Early. This is the project skeleton; the harness is being ported next. Nothing here talks to a model yet.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run local-agent --version
uv run pytest
uv run ruff check
```

## License

MIT
