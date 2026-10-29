"""
`local-agent review ...`: what waits for the owner's approval, and the decision.

    local-agent review list              pending items (--all: decided ones too)
    local-agent review show 12           one item in full
    local-agent review approve 12        apply it (a memory write is saved)
    local-agent review reject 12         drop it
"""

import sys

from local_agent.config import Home
from local_agent.review import queue
from local_agent.store import SQLiteStore

PREVIEW = 90


def add_arguments(subparsers) -> None:
    review = subparsers.add_parser('review', help='what waits for your approval (memory from third-party text...)'
                                   ).add_subparsers(dest='review_command', required=True)
    listing = review.add_parser('list', help='pending items (--all: approved and rejected too)')
    listing.add_argument('--all', action='store_true')
    listing.add_argument('--kind', default=None, help='only this kind, e.g. memory')
    for verb, text in (('show', 'one item in full'), ('approve', 'apply it'), ('reject', 'drop it')):
        parser = review.add_parser(verb, help=text)
        parser.add_argument('item_id', type=int)
        if verb == 'reject':
            parser.add_argument('--reason', default='')


def _line(item: queue.ReviewItem) -> str:
    summary = item.summary if len(item.summary) <= PREVIEW else item.summary[:PREVIEW - 3] + '...'
    status = '' if item.status == queue.PENDING else f'  [{item.status}]'
    return f'#{item.id:<4} {item.kind:<9} {item.created_at.astimezone():%Y-%m-%d %H:%M}  {summary}{status}'


def _origin(item: queue.ReviewItem) -> str:
    run = f'run {item.run_id[:8]}' if item.run_id else 'between runs'
    return f'{item.source}, {run}' + (f', after reading {", ".join(item.taint)}' if item.taint else '')


def list_items(store, show_all: bool, kind: str | None) -> int:
    items = queue.ReviewQueue(store).list('all' if show_all else queue.PENDING, kind)
    if not items:
        print('Nothing waits for your review.' if not show_all else 'Nothing has been queued for review.')
        return 0
    for item in items:
        print(_line(item))
    if not show_all:
        print('\nSee one with: local-agent review show <id>; then approve <id> or reject <id>.')
    return 0


def show(store, item_id: int) -> int:
    item = queue.ReviewQueue(store).get(item_id)
    if item is None:
        print(f'There is no review item #{item_id}.', file=sys.stderr)
        return 1
    print(f'#{item.id} {item.kind}, {item.status}, queued {item.created_at.astimezone():%Y-%m-%d %H:%M}')
    print(f'From: {_origin(item)}')
    if item.decided_at:
        print(f'Decided {item.decided_at.astimezone():%Y-%m-%d %H:%M} ({item.decided_by})'
              + (f': {item.result}' if item.result else ''))
    print('This came from text someone else wrote. Approve it only if you want it as your own.\n')
    print(queue.describe(item))
    return 0


def decide(store, home: Home, item_id: int, approve: bool, reason: str = '') -> int:
    try:
        item = (queue.approve(store, home, item_id, 'cli') if approve else
                queue.reject(store, home, item_id, 'cli', reason))
    except (queue.ReviewError, queue.NotFound) as e:
        print(e, file=sys.stderr)
        return 1
    print(f'Approved #{item.id}: {item.result}' if approve else f'Rejected #{item.id}: nothing of it was used.')
    return 0


def main(home: Home, args) -> int:
    home.ensure()
    store = SQLiteStore(home.db_path)
    try:
        if args.review_command == 'list':
            return list_items(store, args.all, args.kind)
        if args.review_command == 'show':
            return show(store, args.item_id)
        return decide(store, home, args.item_id, args.review_command == 'approve', getattr(args, 'reason', ''))
    finally:
        store.close()
