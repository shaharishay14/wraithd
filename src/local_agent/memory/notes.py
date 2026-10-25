"""
Memory notes as real markdown files: workspace/memory/<folder>/<name>.md. The owner can open, edit or delete
them in any editor; the agent sees the change on its next run.

Each file has a small frontmatter block for what the harness needs to know about it:

    ---
    title: Dana
    trust: low
    source: agent
    source_detail: run 3f2a..., after reading web_search
    ---
    - Designer at Acme, works with me on [[website-redesign]]

A file without frontmatter is the owner's own note: high trust. Deleting the `trust: low` line is how the
owner vouches for a note, like editing it on agent-studio's Memory page did.
"""

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from local_agent.memory import vault

SOURCE_USER = 'user'  # written or edited by the owner
SOURCE_DISTILL = 'distill'  # extracted from a conversation by distill
SOURCE_AGENT = 'agent'  # saved by the agent during a run

MAX_NOTES = 2000  # a personal vault; past this, only the most recently updated notes are searched
FRONTMATTER = re.compile(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', re.DOTALL)
TITLE_HEADING = re.compile(r'^#\s+(.+)$', re.MULTILINE)


@dataclass
class Note:
    path: str  # folder/name.md, always normalized (vault.normalize_path)
    title: str
    body: str = ''
    trust: str = vault.HIGH
    source: str = SOURCE_USER
    source_detail: str = ''
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def links(self) -> list[str]:
        return vault.extract_links(self.body)


def _default_title(path: str) -> str:
    return vault.stem_of(path).replace('-', ' ').title()


def parse(path: str, text: str, updated_at: datetime) -> Note:
    match = FRONTMATTER.match(text)
    meta, body = {}, text
    if match:
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict):
            meta, body = loaded, match.group(2)
    heading = TITLE_HEADING.search(body)
    title = meta.get('title') or (heading.group(1).strip() if heading else _default_title(path))
    return Note(path=path, title=str(title),
                body=body.strip(), trust=vault.LOW if meta.get('trust') == vault.LOW else vault.HIGH,
                source=str(meta.get('source') or SOURCE_USER), source_detail=str(meta.get('source_detail') or ''),
                updated_at=updated_at)


def render(note: Note) -> str:
    meta = {'title': note.title, 'source': note.source}
    if note.trust == vault.LOW:
        meta['trust'] = vault.LOW
    if note.source_detail:
        meta['source_detail'] = note.source_detail
    return f'---\n{yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()}\n---\n{note.body.strip()}\n'


class Vault:
    """The notes in one memory folder."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _file(self, path: str) -> Path:
        # normalize_path only produces folder/kebab-name.md inside the schema, so this can't leave the folder
        return self.root / vault.normalize_path(path)

    def load(self) -> list[Note]:
        """Every note in the schema's folders, most recently updated first."""
        notes = []
        for folder in vault.FOLDERS:
            for file in (self.root / folder).glob('*.md'):
                try:
                    path = vault.normalize_path(f'{folder}/{file.stem}')
                except ValueError:
                    continue
                if path != f'{folder}/{file.name}':
                    continue  # a name the agent couldn't address ("My Notes.md"): leave it alone
                updated = datetime.fromtimestamp(file.stat().st_mtime, UTC)
                notes.append(parse(path, file.read_text(errors='replace'), updated))
        return sorted(notes, key=lambda note: note.updated_at, reverse=True)[:MAX_NOTES]

    def get(self, path: str) -> Note | None:
        file = self._file(path)
        if not file.exists():
            return None
        return parse(vault.normalize_path(path), file.read_text(errors='replace'),
                     datetime.fromtimestamp(file.stat().st_mtime, UTC))

    def save(self, note: Note) -> None:
        """Writes the note atomically, so an editor or a crash never sees half a file."""
        file = self._file(note.path)
        file.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=file.parent, prefix='.', suffix='.tmp')
        try:
            with os.fdopen(handle, 'w') as out:
                out.write(render(note))
            os.replace(temporary, file)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
