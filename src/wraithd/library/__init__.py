"""
The library: what wraithd has read (posts, repos, articles), each with a short note, and the ideas it has suggested
to the owner, each with a status. Radar (skills/library/radar/) reads with it so it never reads the same item twice
and never suggests the same idea twice; later questions ("that post about X", "tell me more about #2") are answered
from the notes.

Two tables in state.db (store/modules.py, capabilities_001): capabilities_library_items and
capabilities_library_ideas. Notes are wraithd's own words about someone else's text: they're redacted before they're
stored, and reading them back taints the run like the page they came from (the gate's UNTRUSTED_OUTPUT_BUILTINS).
They never reach memory.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from wraithd.core.secrets import redact_text
from wraithd.store.modules import ModuleTables

KINDS = ('post', 'repo', 'article')
SOURCES = ('x', 'bookmark', 'github', 'web')
SUGGESTED, ACCEPTED, DECLINED, BUILT = 'suggested', 'accepted', 'declined', 'built'
STATUSES = (SUGGESTED, ACCEPTED, DECLINED, BUILT)
SIZES = ('S', 'M', 'L')
NOTE_CHARS = 600
PLAN_CHARS = 400
TITLE_CHARS = 200
MAX_IDS = 200  # library_seen in one call

ITEMS = 'capabilities_library_items'
IDEAS = 'capabilities_library_ideas'

_X_HOSTS = {'x.com', 'twitter.com', 'mobile.twitter.com', 'www.x.com', 'www.twitter.com', 'mobile.x.com'}
_GH_HOSTS = {'github.com', 'www.github.com'}
_X_STATUS = re.compile(r'/status(?:es)?/(\d+)')
_GH_NAME = re.compile(r'^[a-z0-9._-]+$')  # owner and repo names, lowercased
# github.com/<these>/... are GitHub's own pages, not a repo
_GH_PAGES = {'about', 'apps', 'collections', 'enterprise', 'events', 'explore', 'features', 'login', 'marketplace',
             'notifications', 'orgs', 'pricing', 'search', 'settings', 'sponsors', 'topics', 'trending', 'users'}
_WORD = re.compile(r'[\w@#.+-]+')


class LibraryError(ValueError):
    """Something the model can fix: an id it can't read, a status that doesn't exist."""


# --- ids -------------------------------------------------------------------------------------------------------

def _url_id(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        raise LibraryError(f'{url!r} is not an http(s) URL.')
    host = parts.hostname.lower()
    path = parts.path or '/'
    if host in _X_HOSTS and (status := _X_STATUS.search(path)):
        return f'x:{status.group(1)}'
    segments = [segment for segment in path.split('/') if segment]
    if host in _GH_HOSTS and len(segments) == 2 and segments[0].lower() not in _GH_PAGES:
        return _gh_id('/'.join(segments))
    netloc = host + (f':{parts.port}' if parts.port else '')
    query = urlencode([(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                       if not key.lower().startswith('utm_')])
    path = path.rstrip('/') if path != '/' else ''
    return 'url:' + urlunsplit((parts.scheme.lower(), netloc, path, query, ''))


def _gh_id(name: str) -> str:
    name = name.strip().strip('/').lower().removesuffix('.git')
    owner, _, repo = name.partition('/')
    if not (_GH_NAME.match(owner) and _GH_NAME.match(repo)) or {owner, repo} & {'.', '..'}:
        raise LibraryError(f'{name!r} is not owner/repo.')
    return f'gh:{owner}/{repo}'


def normalize_id(item_id: str) -> str:
    """
    The stable id of an item: "x:<post id>" (digits only), "gh:<owner>/<repo>" (lowercase), "url:<url>" (scheme and
    host lowercased, no fragment, no utm_* parameters, no trailing slash). An X post or a GitHub repo given as a URL
    gets its x: or gh: id, so the same thing read from two sources is one item. Raises LibraryError.
    """
    text = (item_id or '').strip()
    prefix, _, rest = text.partition(':')
    prefix = prefix.lower()
    if prefix == 'x':
        if rest.lower().startswith(('http://', 'https://')):
            return normalize_id(rest)
        digits = re.sub(r'\D', '', rest)
        if not digits:
            raise LibraryError(f'{item_id!r} has no X post id (x:<digits>).')
        return f'x:{digits}'
    if prefix == 'gh':
        if rest.lower().startswith(('http://', 'https://')):
            parts = urlsplit(rest)
            return _gh_id('/'.join(parts.path.strip('/').split('/')[:2]))
        return _gh_id(rest)
    if prefix == 'url':
        return _url_id(rest)
    if prefix in ('http', 'https'):
        return _url_id(text)
    raise LibraryError(f'{item_id!r} is not an item id: use x:<post id>, gh:<owner>/<repo> or url:<url>.')


# --- the tables ------------------------------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


def _clean(text: Any, limit: int) -> str:
    """Stripped, redacted (core/secrets.py) and cut to limit."""
    text = redact_text(str(text or '').strip())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _title_key(title: str) -> str:
    return ' '.join(re.findall(r'[a-z0-9]+', title.lower()))


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    title: str
    url: str
    author: str
    note: str
    source: str
    read_at: str

    def shown(self) -> dict[str, str]:
        return {'id': self.id, 'kind': self.kind, 'title': self.title, 'url': self.url, 'author': self.author,
                'note': self.note, 'read_at': self.read_at}


@dataclass(frozen=True)
class Idea:
    id: str
    title: str
    plan: str
    files: list[str]
    size: str
    source_ids: list[str]
    status: str
    created_at: str
    updated_at: str

    def shown(self) -> dict[str, Any]:
        return {'id': self.id, 'title': self.title, 'plan': self.plan, 'files': self.files, 'size': self.size,
                'source_ids': self.source_ids, 'status': self.status, 'created_at': self.created_at}


def _item(row: dict) -> Item:
    return Item(**{key: row[key] for key in Item.__dataclass_fields__})


def _idea(row: dict) -> Idea:
    return Idea(id=row['id'], title=row['title'], plan=row['plan'], files=json.loads(row['files']), size=row['size'],
                source_ids=json.loads(row['source_ids']), status=row['status'], created_at=row['created_at'],
                updated_at=row['updated_at'])


def _terms(query: str) -> list[str]:
    terms = []
    for word in _WORD.findall((query or '').lower()):
        word = word.strip('.+-').lstrip('@#')
        if len(word) >= 2 and word not in terms:
            terms.append(word)
    return terms


def _score(item: Item, terms: list[str], phrase: str) -> float:
    fields = ((item.title.lower(), 3.0), (item.author.lower(), 3.0), (item.url.lower(), 1.5),
              (item.note.lower(), 1.0), (item.id.lower(), 1.5))
    score = 0.0
    matched = 0
    for term in terms:
        hit = sum(weight for text, weight in fields if term in text)
        matched += bool(hit)
        score += hit
    if not matched:
        return 0.0
    score *= matched / len(terms)  # an item with every word comes first
    if len(terms) > 1 and phrase and any(phrase in text for text, _ in fields):
        score += 2.0
    return score


class Library(ModuleTables):
    # --- items -------------------------------------------------------------------------------------------------

    def seen(self, ids: list[str]) -> dict[str, list[str]]:
        """Which of these ids were read before (one query), as normalized ids; ids it can't read under "invalid"."""
        wanted, invalid = [], []
        for item_id in ids or []:
            try:
                normalized = normalize_id(str(item_id))
            except LibraryError:
                invalid.append(str(item_id))
                continue
            if normalized not in wanted:
                wanted.append(normalized)
        known = set()
        if wanted:
            marks = ','.join('?' * len(wanted))
            known = {row['id'] for row in self.query(f'SELECT id FROM {ITEMS} WHERE id IN ({marks})', wanted)}
        result = {'new': [i for i in wanted if i not in known], 'seen': [i for i in wanted if i in known]}
        if invalid:
            result['invalid'] = invalid
        return result

    def save(self, item_id: str, kind: str, title: str, url: str, note: str, source: str, author: str = '',
             now: str | None = None) -> Item:
        """Saves an item read (again: the note and title are replaced, the first read_at stays). Redacted."""
        normalized = normalize_id(item_id)
        if kind not in KINDS:
            raise LibraryError(f'kind {kind!r} is not one of {", ".join(KINDS)}.')
        if source not in SOURCES:
            raise LibraryError(f'source {source!r} is not one of {", ".join(SOURCES)}.')
        title = _clean(title, TITLE_CHARS)
        if not title:
            raise LibraryError('The title is empty.')
        url = _clean(url, 500) or (normalized[4:] if normalized.startswith('url:') else '')
        stamp = now or _now()
        self.execute(
            f'INSERT INTO {ITEMS} (id, kind, title, url, author, note, source, read_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET kind = excluded.kind, '
            'title = excluded.title, url = excluded.url, author = excluded.author, note = excluded.note, '
            'source = excluded.source, updated_at = excluded.updated_at',
            (normalized, kind, title, url, _clean(author, 100), _clean(note, NOTE_CHARS), source, stamp, stamp))
        return self.get(normalized)

    def get(self, item_id: str) -> Item | None:
        rows = self.query(f'SELECT * FROM {ITEMS} WHERE id = ?', (normalize_id(item_id),))
        return _item(rows[0]) if rows else None

    def recent(self, limit: int = 20, kind: str | None = None) -> list[Item]:
        where, params = ('WHERE kind = ?', [kind]) if kind else ('', [])
        return [_item(row) for row in self.query(
            f'SELECT * FROM {ITEMS} {where} ORDER BY read_at DESC, rowid DESC LIMIT ?', [*params, limit])]

    def search(self, query: str, limit: int = 5) -> list[Item]:
        """
        Items that match the words of the query in their title, author, note, url or id, best first: weighted hits
        (title and author count most), scaled by how many of the words matched, a bonus for the whole phrase; newest
        first among equals. An empty query gives the newest items. One LIKE prefilter, then scoring in Python.
        """
        terms = _terms(query)
        if not terms:
            return self.recent(limit)
        columns = ('title', 'author', 'note', 'url', 'id')
        clauses = ' OR '.join(f"lower({column}) LIKE ? ESCAPE '\\'" for _ in terms for column in columns)
        params = [f'%{_escaped(term)}%' for term in terms for _ in columns]
        rows = [_item(row) for row in self.query(
            f'SELECT * FROM {ITEMS} WHERE {clauses} ORDER BY read_at DESC, rowid DESC', params)]
        phrase = ' '.join(terms)
        scored = [(score, -index, item) for index, item in enumerate(rows) if (score := _score(item, terms, phrase))]
        scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return [item for _, _, item in scored[:limit]]

    def count(self) -> int:
        return self.query(f'SELECT count(*) AS n FROM {ITEMS}')[0]['n']

    # --- ideas -------------------------------------------------------------------------------------------------

    def add_idea(self, title: str, plan: str, files: list[str], size: str, source_ids: list[str],
                 day: date) -> tuple[Idea, bool]:
        """
        Saves an idea as suggested, with the id "<local date>#<n>" (n counts from 1 each day). An idea whose title is
        the same words as one already kept isn't added again: (that idea, False).
        """
        title = _clean(title, TITLE_CHARS)
        if not title:
            raise LibraryError('The idea needs a title.')
        size = str(size or '').strip().upper()
        if size not in SIZES:
            raise LibraryError(f'size {size!r} is not one of S, M, L.')
        files = [_clean(path, 200) for path in (files or []) if str(path or '').strip()][:20]
        sources = []
        for source_id in source_ids or []:
            try:
                normalized = normalize_id(str(source_id))
            except LibraryError:
                continue
            if normalized not in sources:
                sources.append(normalized)
        key = _title_key(title)
        stamp = _now()
        with self.store.transaction() as db:
            existing = db.execute(f'SELECT * FROM {IDEAS} WHERE title_key = ?', (key,)).fetchone()
            if existing:
                return _idea(dict(existing)), False
            day_text = day.isoformat()
            n = db.execute(f'SELECT coalesce(max(n), 0) + 1 FROM {IDEAS} WHERE day = ?', (day_text,)).fetchone()[0]
            idea_id = f'{day_text}#{n}'
            db.execute(
                f'INSERT INTO {IDEAS} (id, day, n, title, title_key, plan, files, size, source_ids, status, '
                'created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (idea_id, day_text, n, title, key, _clean(plan, PLAN_CHARS), json.dumps(files), size,
                 json.dumps(sources), SUGGESTED, stamp, stamp))
        return self.idea(idea_id), True

    def idea(self, idea_id: str) -> Idea | None:
        rows = self.query(f'SELECT * FROM {IDEAS} WHERE id = ?', (str(idea_id or '').strip(),))
        return _idea(rows[0]) if rows else None

    def ideas(self, status: str | None = None, limit: int = 50) -> list[Idea]:
        """Newest day first, in the order they were suggested that day."""
        if status and status not in STATUSES:
            raise LibraryError(f'status {status!r} is not one of {", ".join(STATUSES)}.')
        where, params = ('WHERE status = ?', [status]) if status else ('', [])
        return [_idea(row) for row in self.query(
            f'SELECT * FROM {IDEAS} {where} ORDER BY day DESC, n ASC LIMIT ?', [*params, limit])]

    def set_status(self, idea_id: str, status: str) -> tuple[Idea, str]:
        """(the idea, its previous status). Raises LibraryError for an unknown id or status."""
        if status not in STATUSES:
            raise LibraryError(f'status {status!r} is not one of {", ".join(STATUSES)}.')
        idea = self.idea(idea_id)
        if idea is None:
            raise LibraryError(f'There is no idea {idea_id!r}.')
        self.execute(f'UPDATE {IDEAS} SET status = ?, updated_at = ? WHERE id = ?', (status, _now(), idea.id))
        return self.idea(idea.id), idea.status


def _escaped(term: str) -> str:
    return term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
