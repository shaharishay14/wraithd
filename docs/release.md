# Releasing

**Files:** `scripts/release.sh`, `.github/workflows/release.yml`, `scripts/install.sh`, `pyproject.toml`

## One-liner

> Bump the version, tag `v<version>`, push the tag: CI builds the wheel (with the web UI) and publishes it to PyPI
> with trusted publishing, no token anywhere.

## How it works

- **The build** is `scripts/release.sh` ([Installing and running](install.md)): the web UI from `frontend/` into
  `src/wraithd/ui/`, then the sdist and wheel in `dist/`, checked for `wraithd/ui/index.html`. The sdist carries
  only `src/`, `tests/`, `docs/*.md`, `scripts/release.sh`, the README, LICENSE and `uv.lock`.
- **The workflow** (`.github/workflows/release.yml`):
  - On a tag `v*`: checks the tag equals `v` + the version in `pyproject.toml`, runs ruff and pytest,
    `scripts/release.sh`, `twine check --strict`, installs the wheel as a uv tool and runs `wraithd --help`, then the
    `pypi` job publishes `dist/` with `pypa/gh-action-pypi-publish` in the GitHub environment `pypi`.
  - By hand (Actions > Release > Run workflow): the same build, published to TestPyPI from the environment
    `testpypi`. TestPyPI refuses a version it already has, so a second dry run needs a new version
    (for example `0.0.2.dev1`, not tagged).
  - Trusted publishing: PyPI trusts this repo's `release.yml` in that environment, and the job gets a short-lived
    token through OIDC (`id-token: write`). No API token is stored in GitHub.
- **The curl installer** (`scripts/install.sh`) is served from `main` on raw.githubusercontent.com, so it's live
  as soon as it's merged; it installs whatever PyPI has.
- **Homebrew:** none (skipped on 2026-11-03).

## One time (the owner)

1. **PyPI account.** Sign up at [pypi.org](https://pypi.org/account/register/) and turn on 2FA (required to
   publish). Same at [test.pypi.org](https://test.pypi.org/account/register/) (a separate account).
2. **Trusted publisher on PyPI**, before the first upload (a "pending publisher" creates the project on the first
   publish): pypi.org > Your account > Publishing > Add a new pending publisher > GitHub:
   - PyPI project name `wraithd`, owner `shaharishay14`, repository `wraithd`,
     workflow `release.yml`, environment `pypi`.
   Do the same on test.pypi.org with environment `testpypi`.
3. **GitHub environments.** Repo > Settings > Environments: create `pypi` (optionally: required reviewer = you, and
   deployment tags `v*` only) and `testpypi`.
4. Check the name is still free just before: `curl -s -o /dev/null -w '%{http_code}' https://pypi.org/pypi/wraithd/json`
   prints `404` while nobody has it (it was free on 2026-11-03; 0.0.1 was published the same day).

## Each release

1. Optional dry run: Actions > Release > Run workflow (TestPyPI), then
   `uv tool install --python 3.12 --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ --index-strategy unsafe-best-match wraithd`
   in a throwaway `UV_TOOL_DIR`.
2. Set `version` in `pyproject.toml` (and run `uv lock`), and in `CHANGELOG.md` replace `(unreleased)` with the
   date. Merge to `main`.
3. Tag and push: `git tag v<version> && git push origin v<version>`. Watch the Release workflow; approve the `pypi`
   environment if you set a reviewer.
4. Check: `uvx --python 3.12 wraithd --version` prints the new version.
5. Optional: a GitHub release from the tag, with `dist/` from the workflow's artifact.

## Limits

- A version on PyPI can't be replaced, only yanked: a broken release means a new version.
- `wraithd update` doesn't upgrade pip installs; it says so and leaves them to their own tool.
