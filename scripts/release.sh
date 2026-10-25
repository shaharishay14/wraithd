#!/usr/bin/env bash
# The release build: the web UI from frontend/, copied into the package, then the sdist and wheel in dist/.
#
#   scripts/release.sh
#
# npm runs on a copy of frontend/ in a temp folder, never in the checkout. The built UI goes to src/local_agent/ui/
# (gitignored); hatch ships it as an artifact. The wheel is checked for ui/index.html before the script says done.
# Publishing is a separate, manual step (uv publish).
set -euo pipefail

fail() { echo "release: $*" >&2; exit 1; }

root=$(cd "$(dirname "$0")/.." && pwd)
for tool in node npm uv; do
    command -v "$tool" >/dev/null || fail "$tool is not installed"
done
[ -f "$root/frontend/package.json" ] || fail "frontend/package.json is missing"
[ -f "$root/frontend/package-lock.json" ] || fail "frontend/package-lock.json is missing (npm ci needs it)"
[ -d "$root/tests/core/runs" ] || fail "tests/core/runs is missing (the UI's mock data reads it)"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The copy keeps the repo layout: the UI's mock backend reads the recorded runs from ../tests/core/runs
echo "release: building the web UI on a copy in $work"
mkdir -p "$work/tests/core"
tar -C "$root" --exclude node_modules --exclude dist -cf - frontend | tar -C "$work" -xf -
cp -R "$root/tests/core/runs" "$work/tests/core/runs"
(cd "$work/frontend" && npm ci --no-audit --no-fund && npm run build)
[ -f "$work/frontend/dist/index.html" ] || fail "the UI build made no dist/index.html"

ui="$root/src/local_agent/ui"
rm -rf "$ui"
cp -R "$work/frontend/dist" "$ui"
echo "release: copied the UI into src/local_agent/ui/"

rm -rf "$root/dist"
(cd "$root" && uv build --out-dir dist)

wheel=$(ls "$root"/dist/*.whl 2>/dev/null | head -n 1 || true)
[ -n "$wheel" ] || fail "uv build made no wheel"
uv run --no-project python - "$wheel" <<'PY' || fail "the wheel has no local_agent/ui/index.html"
import sys
import zipfile

names = zipfile.ZipFile(sys.argv[1]).namelist()
sys.exit(0 if 'local_agent/ui/index.html' in names else 1)
PY
echo "release: done: $(basename "$wheel") includes the web UI"
