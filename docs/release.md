# Releasing

**Files:** `scripts/release.sh`, `.github/workflows/release.yml`, `scripts/install.sh`, `packaging/homebrew/wraithd.rb`, `pyproject.toml`

## One-liner

> Bump the version, tag `v<version>`, push the tag: CI builds the wheel (with the web UI) and publishes it to PyPI
> with trusted publishing, no token anywhere. Then update the Homebrew formula's `url` and `sha256`.

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
- **Homebrew** (`packaging/homebrew/wraithd.rb`) is a thin formula for the owner's tap: it downloads the release
  wheel from PyPI and has uv install it, with its dependencies, into a virtualenv under the formula's `libexec` on
  `python@3.12`. A classic virtualenv formula would list each of the ~75 dependencies as a resource (several are
  Rust builds) and need them refreshed every release; this one changes two lines. The trade-off: dependencies are
  resolved from PyPI at install time, like `uv tool install`, rather than pinned in the formula.

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
4. **The tap.** Create the public repo `shaharishay14/homebrew-tap` with a `Formula/` folder. Users then run
   `brew install shaharishay14/tap/wraithd`.
5. Check the name is still free just before: `curl -s -o /dev/null -w '%{http_code}' https://pypi.org/pypi/wraithd/json`
   prints `404` while nobody has it (it was free on 2026-11-03).

## Each release

1. Optional dry run: Actions > Release > Run workflow (TestPyPI), then
   `uv tool install --python 3.12 --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ --index-strategy unsafe-best-match wraithd`
   in a throwaway `UV_TOOL_DIR`.
2. Set `version` in `pyproject.toml` (and run `uv lock`), merge to `main`.
3. Tag and push: `git tag v0.0.1 && git push origin v0.0.1`. Watch the Release workflow; approve the `pypi`
   environment if you set a reviewer.
4. Check: `uvx --python 3.12 wraithd --version` prints the new version.
5. Homebrew: in `packaging/homebrew/wraithd.rb` set `url` to the new wheel
   (`https://files.pythonhosted.org/packages/py3/w/wraithd/wraithd-<version>-py3-none-any.whl`) and `sha256` to
   ```sh
   curl -s https://pypi.org/pypi/wraithd/<version>/json | jq -r '.urls[] | select(.packagetype=="bdist_wheel") | .digests.sha256'
   ```
   commit it here, copy it to `homebrew-tap/Formula/wraithd.rb`, push the tap, then
   `brew update && brew install shaharishay14/tap/wraithd && brew test wraithd`.
6. First release only: drop the "after the first release" notes from the README and [install.md](install.md), and add
   the PyPI badge (`https://img.shields.io/pypi/v/wraithd.svg?color=111111`, linking to pypi.org/project/wraithd).
7. Optional: a GitHub release from the tag, with `dist/` from the workflow's artifact.

## Limits

- A version on PyPI can't be replaced, only yanked: a broken release means a new version.
- The formula's placeholder `sha256` (all zeros) fails `brew install` until step 5 of the first release.
- `wraithd update` doesn't upgrade Homebrew or pip installs; it says so and leaves them to their own tool.
