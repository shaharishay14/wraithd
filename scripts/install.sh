#!/bin/sh
# Installs wraithd for the current user, without sudo:
#
#   curl -LsSf https://raw.githubusercontent.com/shaharishay14/wraithd/main/scripts/install.sh | sh
#
# What it does, and nothing else:
#   1. If uv isn't on PATH (or in ~/.local/bin or ~/.cargo/bin), installs it with Astral's own installer
#      (https://astral.sh/uv/install.sh), which puts it in ~/.local/bin.
#   2. Runs `uv tool install wraithd` (or `uv tool upgrade wraithd` if it's already installed). uv gives wraithd
#      its own environment on Python 3.12 (the version CI tests; uv downloads it if the machine has none).
#      Nothing goes into the system Python. (Without --python, uv may pick an older system Python and fail.)
#   3. Prints the next step: `wraithd onboard`.
#
# Settings, all optional:
#   WRAITHD_VERSION=0.0.1   install that version instead of the latest
#   WRAITHD_EXTRAS=whisper  install extras too (comma-separated: whisper, browser)
#   WRAITHD_NO_UV_INSTALL=1 don't install uv; stop if it's missing
#
# It changes no shell profile itself. Astral's installer may add ~/.local/bin to your PATH (it says so when it does).
set -eu

say() { printf 'wraithd install: %s\n' "$*"; }
fail() { printf 'wraithd install: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
    Darwin | Linux) ;;
    *) fail "wraithd runs on macOS and Linux; on Windows, use WSL" ;;
esac

if [ "$(id -u)" = 0 ]; then
    say "running as root: wraithd will be installed for root only. Run this as your own user unless you mean that."
fi

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    for dir in "${XDG_BIN_HOME:-}" "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [ -n "$dir" ] && [ -x "$dir/uv" ]; then
            printf '%s\n' "$dir/uv"
            return 0
        fi
    done
    return 1
}

if ! uv=$(find_uv); then
    [ -z "${WRAITHD_NO_UV_INSTALL:-}" ] || fail "uv is not installed (https://docs.astral.sh/uv/) and WRAITHD_NO_UV_INSTALL is set"
    say "uv is not installed; installing it with https://astral.sh/uv/install.sh"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        fail "need curl or wget to install uv"
    fi
    uv=$(find_uv) || fail "uv was installed but isn't on PATH or in ~/.local/bin; open a new terminal and run this again"
fi
say "using uv at $uv"

spec=wraithd
if [ -n "${WRAITHD_EXTRAS:-}" ]; then
    spec="wraithd[$WRAITHD_EXTRAS]"
fi
if [ -n "${WRAITHD_VERSION:-}" ]; then
    spec="$spec==$WRAITHD_VERSION"
fi

if "$uv" tool list 2>/dev/null | grep -q '^wraithd '; then
    if [ -n "${WRAITHD_VERSION:-}${WRAITHD_EXTRAS:-}" ]; then
        say "reinstalling $spec"
        "$uv" tool install --python 3.12 --force "$spec"
    else
        say "wraithd is already installed; upgrading it"
        "$uv" tool upgrade wraithd
    fi
else
    say "installing $spec"
    "$uv" tool install --python 3.12 "$spec"
fi

bin_dir=$("$uv" tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")
echo
case ":$PATH:" in
*":$bin_dir:"*)
    say "done. Next: wraithd onboard"
    ;;
*)
    say "done. $bin_dir is not on your PATH yet: run \`$uv tool update-shell\` and open a new terminal,"
    say "or run it directly: $bin_dir/wraithd onboard"
    ;;
esac
