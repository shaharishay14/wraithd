"""
wraithd_index: what wraithd is today, so the agent doesn't suggest what's already built. From the installed package:
each module's path and the first line of its docstring. From a source checkout (docs/ next to src/): each note's title
and one-liner, and the first lines of the CHANGELOG. Read-only, no path argument, nothing outside the package and its
checkout; built once per process and cut to MAX_CHARS (the most detail that fits).
"""

import ast
import importlib.resources
import json
import re
from functools import cache
from pathlib import Path

import wraithd

# The JSON the model gets, about 2,500 tokens. The paths and titles alone (modules one line per package, ~46 notes)
# take ~5,000 characters, so 6,000 would leave the notes' one-liners a dozen characters each.
MAX_CHARS = 10_000
CHANGELOG_LINES = 60
# From the most detail to the least, the first that fits is used: module summary and note one-liner lengths, changelog
# characters, and whether modules are one entry per package (its own line and its file names) instead of per file.
# The notes' one-liners say best what's built, so they're kept longest.
LEVELS = (
    (90, 160, 4000, False),
    (60, 120, 2500, True),
    (0, 100, 1500, True),
    (0, 70, 1000, True),
    (0, 50, 600, True),
    (0, 0, 400, True),
)


def _first_line(text: str | None) -> str:
    for line in (text or '').strip().splitlines():
        if line.strip():
            return line.strip()
    return ''


def _cut(text: str, limit: int) -> str:
    if limit <= 0:
        return ''
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _modules() -> list[dict[str, str]]:
    """Every .py file in the package: its path as it is in the repo (src/wraithd/...) and its docstring's first line."""
    found = []

    def walk(folder, prefix: str) -> None:
        for entry in sorted(folder.iterdir(), key=lambda e: e.name):
            if entry.name.startswith(('.', '_')) and entry.name != '__init__.py' or entry.name == '__pycache__':
                continue
            if entry.is_dir():
                walk(entry, f'{prefix}{entry.name}/')
            elif entry.name.endswith('.py'):
                try:
                    summary = _first_line(ast.get_docstring(ast.parse(entry.read_text(encoding='utf-8'))))
                except (SyntaxError, UnicodeDecodeError, OSError):
                    summary = ''
                found.append({'path': f'{prefix}{entry.name}', 'summary': summary})

    walk(importlib.resources.files('wraithd'), 'src/wraithd/')
    return found


def checkout_root() -> Path | None:
    """The repo the package runs from (src/wraithd with docs/ and pyproject.toml beside src/), else None."""
    package = Path(wraithd.__file__).resolve().parent
    root = package.parent.parent
    if package.parent.name == 'src' and (root / 'docs').is_dir() and (root / 'pyproject.toml').is_file():
        return root
    return None


def _one_liner(text: str) -> str:
    """The quote under "## One-liner", else the first plain paragraph after the title."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower() == '## one-liner':
            quote = []
            for after in lines[i + 1:]:
                if after.startswith('#'):
                    break
                if after.startswith('>'):
                    quote.append(after.lstrip('> ').strip())
                elif quote:
                    break
            if quote:
                return ' '.join(part for part in quote if part)
    paragraph = []
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith(('#', '**', '|', '```', '>', '-', '!', '<')):
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    return ' '.join(paragraph)


def _docs(root: Path | None) -> list[dict[str, str]]:
    if root is None:
        return []
    docs = []
    for path in sorted((root / 'docs').glob('*.md')):
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        title = next((line[2:].strip() for line in text.splitlines() if line.startswith('# ')), path.stem)
        docs.append({'path': f'docs/{path.name}', 'title': title, 'one_liner': re.sub(r'\s+', ' ', _one_liner(text))})
    return docs


def _changelog(root: Path | None) -> str:
    if root is None:
        return ''
    try:
        lines = (root / 'CHANGELOG.md').read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeDecodeError):
        return ''
    return '\n'.join(lines[:CHANGELOG_LINES]).strip()


def _by_package(modules: list[dict[str, str]], summary_chars: int) -> list[dict[str, str]]:
    """One entry per folder: the package's own line, and the names of its files."""
    folders: dict[str, list[dict[str, str]]] = {}
    for module in modules:
        folder, _, name = module['path'].rpartition('/')
        folders.setdefault(folder, []).append({**module, 'name': name})
    grouped = []
    for folder, files in folders.items():
        init = next((f for f in files if f['name'] == '__init__.py'), None)
        names = [f['name'].removesuffix('.py') for f in files if f['name'] != '__init__.py']
        if folder == 'src/wraithd':  # the top level's modules each stand alone
            grouped += [{'path': f['path'], 'summary': _cut(f['summary'], summary_chars)} for f in files
                        if f['name'] != '__init__.py']
            continue
        summary = _cut(init['summary'], summary_chars) if init else ''
        grouped.append({'path': f'{folder}/', 'summary': f'{summary} [{", ".join(names)}]'.strip()})
    return grouped


def _render(index: dict) -> str:
    return json.dumps(index, ensure_ascii=False, separators=(',', ':'))


@cache
def build(max_chars: int = MAX_CHARS) -> str:
    """The index as compact JSON, at the most detail that fits max_chars."""
    root = checkout_root()
    modules, docs, changelog = _modules(), _docs(root), _changelog(root)
    index = {}
    for summary_chars, liner_chars, changelog_chars, grouped in LEVELS:
        index = {
            'version': wraithd.__version__,
            'modules': (_by_package(modules, summary_chars) if grouped else
                        [{'path': m['path'], 'summary': _cut(m['summary'], summary_chars)} for m in modules]),
            'docs': [{'path': d['path'], 'title': d['title'], 'one_liner': _cut(d['one_liner'], liner_chars)}
                     for d in docs],
            'changelog_head': _cut(changelog, changelog_chars),
        }
        text = _render(index)
        if len(text) <= max_chars:
            return text
    # Still too long: drop entries from the end, and say so
    while len(_render(index)) > max_chars and (index['modules'] or index['docs']):
        index['docs' if len(index['docs']) >= len(index['modules']) else 'modules'].pop()
        index['truncated'] = True
    return _render(index)
