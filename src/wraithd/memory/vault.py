"""
The vault: paths, the keyword map (L1) and search (L2), as plain functions over the owner's notes.

No embeddings. Vector search puts a small model in charge of what the big one gets to see, and needs a
pipeline (chunking, backfills). Instead the agent gets a map of what's there and searches it with BM25, like a
person skimming their own notes. A personal vault is small (hundreds of notes), so ranking in Python is fast,
needs no index to keep in sync with the files, and is easy to test.
"""

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime

# The schema of a personal vault. It does two jobs: it gives notes a predictable place, and it
# tells distill what kind of facts to extract (see distill.py).
FOLDERS = {
    'profile': ('Who the user is: name, role, location, and how they want every answer (length, tone, '
                'language). One note: profile/profile.md, always in the prompt'),
    'people': 'One note per person the user deals with: who they are, how they relate to the user',
    'projects': 'One note per project or ongoing goal: status, key facts, next steps',
    'preferences': 'How the user wants particular things done: formats, tools, times for a kind of task',
    'decisions': 'Decisions the user made, with the reason, so they are not asked again',
    'routines': 'Recurring things: weekly meetings, reports, habits, reminders',
}
PROFILE_PATH = 'profile/profile.md'
HIGH, LOW = 'high', 'low'  # trust

PROFILE_CHARS = 2000      # L0 budget, about 500 tokens
KEYWORDS_PER_FOLDER = 8
SNIPPET_LINES = 3
SNIPPET_CHARS = 200

_STOPWORD_TEXT = '''
a about above after again all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how
i if in into is it its itself just me more most my no nor not now of off on once only or other our out over own
same she should so some such than that the their them then there these they this those through to too under until
up very was we were what when where which while who whom why will with would you your yours md
'''
STOPWORDS = set(_STOPWORD_TEXT.split())

LINK = re.compile(r'\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]')
HEADING = re.compile(r'^\s{0,3}#{1,6}\s+(.*)$')


def words(text: str) -> list[str]:
    """Lowercase content words, with a plural 's' dropped so 'meetings' finds 'meeting'."""
    found = []
    for word in re.findall(r'[a-z0-9]+', (text or '').lower()):
        if word in STOPWORDS or len(word) < 2:
            continue
        found.append(word[:-1] if len(word) > 3 and word.endswith('s') and not word.endswith('ss') else word)
    return found


def normalize_path(path: str) -> str:
    """
    'People/Dana Cohen' -> 'people/dana-cohen.md'. Raises ValueError for paths outside the schema,
    so notes always land in a folder the keyword map and distill know about.
    """
    path = (path or '').strip().lower().removesuffix('.md')
    folder, _, name = path.partition('/')
    if not name:
        raise ValueError(f'A memory path needs a folder: one of {", ".join(FOLDERS)} (e.g. "people/dana.md").')
    if folder not in FOLDERS:
        raise ValueError(f'Unknown memory folder {folder!r}. Use one of: {", ".join(FOLDERS)}.')
    name = re.sub(r'[^a-z0-9]+', '-', name).strip('-')
    if not name:
        raise ValueError('A memory note needs a name, e.g. "people/dana.md".')
    return f'{folder}/{name[:80]}.md'


def folder_of(path: str) -> str:
    return path.split('/', 1)[0]


def stem_of(path: str) -> str:
    return path.rsplit('/', 1)[-1].removesuffix('.md')


def extract_links(body: str) -> list[str]:
    """Targets of [[wiki links]], as written (resolved against the vault when searching)."""
    return sorted({target.strip().lower() for target in LINK.findall(body or '')})


def backlink_counts(notes: Iterable) -> Counter:
    """How many other notes link to each note, matching a link by full path or by name."""
    notes = list(notes)
    by_stem = defaultdict(list)
    for note in notes:
        by_stem[stem_of(note.path)].append(note.path)
    counts = Counter()
    for note in notes:
        for link in note.links or []:
            target = link.removesuffix('.md')
            stem = re.sub(r'[^a-z0-9]+', '-', target)
            for path in ([target + '.md'] if '/' in target else by_stem.get(stem, [])):
                if path != note.path:
                    counts[path] += 1
    return counts


def _weighted_terms(note) -> Counter:
    """Terms of a note, weighted: title and headings 3x, file name 2x, body 1x."""
    terms = Counter()
    for word in words(note.title):
        terms[word] += 3
    for word in words(stem_of(note.path).replace('-', ' ')):
        terms[word] += 2
    for line in (note.body or '').splitlines():
        heading = HEADING.match(line)
        for word in words(heading.group(1) if heading else line):
            terms[word] += 3 if heading else 1
    return terms


# --- L0 and L1 -----------------------------------------------------------------------------------------

def profile_text(notes: Iterable) -> str:
    """
    L0. The profile goes into the system prompt, so a low-trust one (written after the run read third-party
    content) stays out: the model is told it exists and can read it as data instead.
    """
    for note in notes:
        if note.path == PROFILE_PATH:
            if note.trust == LOW:
                return f'(The profile has content from a third-party source: memory_read {PROFILE_PATH} to see it.)'
            body = (note.body or '').strip()
            return body if len(body) <= PROFILE_CHARS else body[:PROFILE_CHARS] + '\n[...] (memory_read for the rest)'
    return ''


def keyword_map(notes: Iterable, per_folder: int = KEYWORDS_PER_FOLDER) -> dict[str, dict]:
    """
    For each folder: how many notes, and the words that set it apart (TF-IDF across folders).
    Words common to every folder say nothing about where to look, so they're dropped.
    """
    folder_terms: dict[str, Counter] = defaultdict(Counter)
    folder_counts = Counter()
    for note in notes:
        folder = folder_of(note.path)
        folder_terms[folder].update(_weighted_terms(note))
        folder_counts[folder] += 1

    total = len(folder_terms)
    document_frequency = Counter(term for terms in folder_terms.values() for term in terms)
    overview = {}
    for folder in sorted(folder_terms):
        scored = []
        for term, weight in folder_terms[folder].items():
            if total > 1 and document_frequency[term] == total:
                continue
            scored.append((weight * math.log(1 + total / document_frequency[term]), term))
        overview[folder] = {'notes': folder_counts[folder],
                            'keywords': [term for _, term in sorted(scored, key=lambda s: (-s[0], s[1]))][:per_folder]}
    return overview


def memory_context(notes: list) -> str:
    """The memory part of the system prompt: L0 (profile) and L1 (keyword map), plus how to go deeper."""
    profile = profile_text(notes)
    overview = keyword_map(notes)
    lines = ['\nMEMORY (what you know about the user, from earlier conversations):']
    lines.append(f'Profile:\n{profile}' if profile else 'Profile: (empty)')
    if overview:
        lines.append('Notes by folder (keywords show what each folder is about):')
        lines += [f'- {folder}/ ({info["notes"]} notes): {", ".join(info["keywords"])}'
                  for folder, info in overview.items()]
        lines.append('To recall something: memory_search with a few words, then memory_read on a result. '
                     'Search before asking the user something they may have told you before.')
    else:
        lines.append('No notes yet.')
    lines.append('To remember a durable fact (a preference, a person, a decision): memory_append. How the user '
                 f'wants every answer goes in {PROFILE_PATH}. Folders: {", ".join(FOLDERS)}. '
                 'Don\'t save one-off details or anything the user asked to keep private.')
    return '\n'.join(lines) + '\n'


# --- L2: search ----------------------------------------------------------------------------------------

def _matching_lines(note, query_words: set) -> list[str]:
    """Only the lines that contain a query word: enough to judge the note, not the whole note."""
    matches = []
    for line in [note.title] + (note.body or '').splitlines():
        if line.strip() and query_words & set(words(line)):
            text = line.strip()
            matches.append(text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS - 3] + '...')
        if len(matches) == SNIPPET_LINES:
            break
    return matches


def search(notes: Iterable, query: str, limit: int = 5, now: datetime | None = None) -> list:
    """
    Notes for a query, best first: BM25 over the weighted terms, then a small boost for notes many
    others link to (they're central) and for recent ones. The scores order the results and are
    not returned: the model should judge the snippets, not anchor on numbers.
    """
    notes = list(notes)
    query_words = set(words(query))
    if not notes or not query_words:
        return []

    terms = {note.path: _weighted_terms(note) for note in notes}
    lengths = {path: sum(counts.values()) for path, counts in terms.items()}
    average = sum(lengths.values()) / len(notes) or 1
    document_frequency = Counter(term for counts in terms.values() for term in counts)
    backlinks = backlink_counts(notes)
    now = now or datetime.now(UTC)

    k1, b = 1.2, 0.75
    ranked = []
    for note in notes:
        score = 0.0
        for word in query_words:
            tf = terms[note.path].get(word, 0)
            if not tf:
                continue
            idf = math.log(1 + (len(notes) - document_frequency[word] + 0.5) / (document_frequency[word] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * lengths[note.path] / average))
        if not score:
            continue
        score *= 1 + 0.1 * min(backlinks[note.path], 5)
        if getattr(note, 'updated_at', None):
            age_days = max((now - note.updated_at).total_seconds() / 86400, 0)
            score *= 1 + 0.2 * math.exp(-age_days / 30)
        ranked.append((-score, note.path, note))

    return [{'path': note.path, 'title': note.title, 'lines': _matching_lines(note, query_words),
             'trust': note.trust}
            for _, _, note in sorted(ranked, key=lambda r: (r[0], r[1]))[:limit]]
