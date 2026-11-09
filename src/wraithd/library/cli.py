"""
`wraithd library ...` and `wraithd ideas ...`: what radar has read and what it has suggested.

    wraithd library                      the latest items read (list; --kind post|repo|article, --limit N)
    wraithd library show x:1850...       one item and its note
    wraithd library search mcp memory    items matching these words, best first
    wraithd ideas                        the ideas suggested, newest first (list; --status S)
    wraithd ideas status 2026-11-09#3 accepted
"""

import sys

from wraithd.config import Home
from wraithd.library import KINDS, STATUSES, Library, LibraryError
from wraithd.store import SQLiteStore

PREVIEW = 80


def add_arguments(subparsers) -> None:
    library = subparsers.add_parser('library', help='what radar has read: posts, repos and articles, with notes')
    commands = library.add_subparsers(dest='library_command')
    listing = commands.add_parser('list', help='the latest items read (the default)')
    listing.add_argument('--kind', choices=KINDS, default=None)
    listing.add_argument('--limit', type=int, default=20)
    commands.add_parser('show', help='one item and its note').add_argument('item_id')
    search = commands.add_parser('search', help='items matching these words, best first')
    search.add_argument('words', nargs='+')
    search.add_argument('--limit', type=int, default=10)

    ideas = subparsers.add_parser('ideas', help='the ideas radar suggested, and their status')
    idea_commands = ideas.add_subparsers(dest='ideas_command')
    idea_list = idea_commands.add_parser('list', help='newest first (the default)')
    idea_list.add_argument('--status', choices=STATUSES, default=None)
    idea_list.add_argument('--limit', type=int, default=50)
    status = idea_commands.add_parser('status', help="set an idea's status")
    status.add_argument('idea_id', help='e.g. 2026-11-09#3')
    status.add_argument('status', choices=STATUSES)


def _short(text: str) -> str:
    text = ' '.join(text.split())
    return text if len(text) <= PREVIEW else text[:PREVIEW - 1] + '…'


def _item_line(item) -> str:
    by = f' ({item.author})' if item.author else ''
    return f'{item.read_at[:10]}  {item.kind:<7} {item.id}\n            {_short(item.title)}{by}'


def _items(items, empty: str) -> int:
    if not items:
        print(empty)
        return 0
    for item in items:
        print(_item_line(item))
    return 0


def _show(library: Library, item_id: str) -> int:
    try:
        item = library.get(item_id)
    except LibraryError as e:
        print(e, file=sys.stderr)
        return 1
    if item is None:
        print(f'Nothing read with the id {item_id!r}.', file=sys.stderr)
        return 1
    print(f'{item.id}  {item.kind} from {item.source}, read {item.read_at[:16].replace("T", " ")} UTC')
    print(item.title + (f' ({item.author})' if item.author else ''))
    if item.url:
        print(item.url)
    print(f'\n{item.note}' if item.note else '\n(no note)')
    return 0


def _ideas(library: Library, status: str | None, limit: int) -> int:
    ideas = library.ideas(status, limit)
    if not ideas:
        print('No ideas yet.' if not status else f'No {status} ideas.')
        return 0
    for idea in ideas:
        files = f'  [{", ".join(idea.files)}]' if idea.files else ''
        print(f'{idea.id:<14} {idea.size}  {idea.status:<9} {_short(idea.title)}')
        if idea.plan:
            print(f'{"":<17}{_short(idea.plan)}{files}')
    print('\nChange one with: wraithd ideas status <id> accepted|declined|built')
    return 0


def _set_status(library: Library, idea_id: str, status: str) -> int:
    try:
        idea, previous = library.set_status(idea_id, status)
    except LibraryError as e:
        print(e, file=sys.stderr)
        return 1
    print(f'{idea.id}: {previous} -> {idea.status}')
    return 0


def main(home: Home, args) -> int:
    home.ensure()
    store = SQLiteStore(home.db_path)
    try:
        library = Library(store)
        if args.command == 'ideas':
            if getattr(args, 'ideas_command', None) == 'status':
                return _set_status(library, args.idea_id, args.status)
            return _ideas(library, getattr(args, 'status', None), getattr(args, 'limit', 50))
        command = getattr(args, 'library_command', None)
        if command == 'show':
            return _show(library, args.item_id)
        if command == 'search':
            return _items(library.search(' '.join(args.words), args.limit), 'Nothing read matches that.')
        return _items(library.recent(getattr(args, 'limit', 20), getattr(args, 'kind', None)),
                      'Nothing read yet. Radar keeps what it reads here.')
    finally:
        store.close()
