<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/shaharishay14/wraithd/main/docs/assets/hero-dark.webp">
    <img alt="wraithd." src="https://raw.githubusercontent.com/shaharishay14/wraithd/main/docs/assets/hero-light.webp" width="100%">
  </picture>
</p>

<p align="center">
  <strong>A personal AI agent that runs on your own machine.</strong><br>
  One package, your model, and you talk to it from the terminal, a local web page or Telegram.
</p>

<p align="center">
  <a href="https://github.com/shaharishay14/wraithd/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/shaharishay14/wraithd/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/shaharishay14/wraithd/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-111111.svg"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-111111.svg">
  <img alt="macOS and Linux" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-111111.svg">
</p>

wraithd is an agent you own, the way you own your notes: one process on your Mac or Linux machine, its state as
plain files and one SQLite file in `~/.wraithd/`, nothing sent anywhere but your model provider (or nowhere, with a
local model through Ollama), Telegram if you pair it, and web searches. The rules that must hold (the permission
gate, taint tracking, the egress guard, budgets) live in code, not in the prompt, and every step of every run is an
event you can inspect.

> **Status:** v0.0.1, in development. The package installs below work after the first PyPI release; until then,
> [install from a checkout](#from-a-checkout).

## Install

Needs macOS or Linux. *After the first release:*

```sh
curl -LsSf https://raw.githubusercontent.com/shaharishay14/wraithd/main/scripts/install.sh | sh
```

The script ([read it first](https://github.com/shaharishay14/wraithd/blob/main/scripts/install.sh)) installs
[uv](https://docs.astral.sh/uv/) if it's missing, then wraithd, without sudo. Or pick your tool:

```sh
uv tool install --python 3.12 wraithd     # uv (downloads Python 3.12 if needed)
pipx install wraithd                      # pipx, with a Python 3.12+
brew install shaharishay14/tap/wraithd    # Homebrew
pip install wraithd                       # into a venv you manage, Python 3.12+
```

Extras: `wraithd[whisper]` transcribes voice notes locally, `wraithd[browser]` adds a real browser.

### From a checkout

```sh
git clone https://github.com/shaharishay14/wraithd
cd wraithd
uv sync
uv run wraithd onboard
```

## Quick start

```sh
wraithd onboard     # model and key, Telegram, the background service, the web UI link
wraithd chat        # talk to it in the terminal
wraithd doctor      # check everything, with a fix for anything wrong
```

`onboard` asks for a provider (OpenAI, Anthropic, Ollama or any OpenAI-compatible endpoint), stores the key in your
OS keychain, optionally sets up your own Telegram bot and installs a background service that starts at login. Run
it again any time: Enter keeps what's set.

## What it does

- **Chat where you are.** The terminal, a local web page, and a Telegram bot that answers only you: photos, voice
  notes and files included.
- **Memory you can read.** Markdown notes you can open and edit, and a short profile of you in every conversation.
- **Skills.** Morning brief, inbox triage, research report, meeting prep, weekly review, loaded only when a request
  matches, plus your own rules.
- **Your apps as MCP servers.** Mail, calendar, notes, anything: `wraithd mcp add`. Read-only tools run; anything
  else asks first, in the terminal, the web UI or Telegram.
- **Schedules and reminders.** Cron or one-time, in your timezone, firing with the terminal closed.
- **The web and files.** Searches with your model provider's own search (no extra key) and reads PDF, Word and
  spreadsheets.
- **Safe by construction.** Unattended runs only do writes you pre-approved; text from an email or page can't
  redirect them; keys stay in the keychain and out of the model's sight; the API listens on 127.0.0.1 with a
  token. `wraithd audit` checks the live setup against these rules.
- **Every step inspectable.** `wraithd runs show <id>` prints a run's timeline: each model call, tool call,
  approval and result.

## Docs

[All docs](https://github.com/shaharishay14/wraithd/blob/main/docs/README.md) ·
[Install and run](https://github.com/shaharishay14/wraithd/blob/main/docs/install.md) ·
[Connect your apps](https://github.com/shaharishay14/wraithd/blob/main/docs/mcp.md) ·
[Security](https://github.com/shaharishay14/wraithd/blob/main/docs/security.md) ·
[Evals](https://github.com/shaharishay14/wraithd/blob/main/docs/evals.md) ·
[Releasing](https://github.com/shaharishay14/wraithd/blob/main/docs/release.md) ·
[Roadmap](https://github.com/shaharishay14/wraithd/blob/main/PLAN.md)

## Development

```sh
uv sync
uv run pytest        # offline: no model, key, ~/.wraithd, keychain or service is touched
uv run ruff check
scripts/release.sh   # the web UI (frontend/) built into the wheel, in dist/
```

The web UI is in `frontend/` and the landing page in `site/` (`npm ci && npm run dev` in either). Issues and pull
requests are welcome; please keep tests offline.

The harness design follows Michael Livshits' writing on agent harnesses
([michaellivs.com/blog](https://michaellivs.com/blog)). [MIT](https://github.com/shaharishay14/wraithd/blob/main/LICENSE) © 2026 Shahar Ishay

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/shaharishay14/wraithd/main/docs/assets/fog-dark.webp">
    <img alt="" src="https://raw.githubusercontent.com/shaharishay14/wraithd/main/docs/assets/fog-light.webp" width="100%">
  </picture>
</p>
