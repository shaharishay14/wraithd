# Contributing to wraithd

Thanks for helping. Bug reports, fixes, docs and tests are all welcome. For a bigger change (a new tool, channel
or provider), open an issue first so we can agree on the shape before you build it.

## Set up

Needs macOS or Linux, [uv](https://docs.astral.sh/uv/) and, for the web UI, Node 22.

```sh
git clone https://github.com/shaharishay14/wraithd
cd wraithd
uv sync
uv run wraithd --help
```

The web UI lives in `frontend/` (React + Vite + TypeScript): `cd frontend && npm ci && npm run dev`.

## Before you open a pull request

Run what CI runs:

```sh
uv run pytest
uv run ruff check
cd frontend && npm ci && npm run build && npm run lint
```

- **Tests stay offline.** No test calls a model, needs a key, uses the network, or touches the real `~/.wraithd`,
  the keychain or a background service. `tests/conftest.py` gives each test a temp home and an in-memory keychain;
  use those. Recorded and scripted runs are explained in [docs/proof.md](docs/proof.md).
- **Hard rules live in code.** Permission checks, taint tracking, the egress guard and budgets are enforced in
  code, not in prompts. A change there needs a test, and an attack test (`tests/attacks/`) if it's a security
  boundary. See [docs/security.md](docs/security.md).
- **Docs with the code.** Each part has a note in [docs/](docs/README.md). If your change alters behaviour, update
  its note in the same pull request.

## Pull requests

- One change per pull request, with a short description of what and why.
- Keep commits readable; they're squashed or merged as they are.
- By contributing you agree your work is released under the [MIT license](LICENSE).

## Security issues

Please don't open a public issue for a vulnerability. Use GitHub's private vulnerability reporting
(Security > Report a vulnerability) on this repository.
