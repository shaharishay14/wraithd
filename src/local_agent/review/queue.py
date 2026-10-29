"""
The review queue: things the agent wants to keep or change that came from someone else's text, waiting for the
owner. Nothing in it takes effect until the owner approves it (`local-agent review approve ID`, or the API).

Each item has a kind ('memory', 'mcp_tool', 'skill'...) and a module registers what approving that kind does:

    queue.register('memory', approve=apply_memory_write, show=describe)

    def apply_memory_write(item, ctx) -> str:   # ctx.home, ctx.store
        ...                                     # raise ReviewError('why not') to refuse; the item stays pending
        return 'saved to people/dana.md'

Content is redacted before it's stored. The owner hears about new items in one short message that carries counts
only, never the queued text: that text is someone else's.
"""

import contextlib
import importlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from local_agent.core import events as ev
from local_agent.core.secrets import redact_text
from local_agent.store.modules import ModuleTables

logger = logging.getLogger(__name__)

PENDING, APPROVING, APPROVED, REJECTED = 'pending', 'approving', 'approved', 'rejected'
STATUSES = (PENDING, APPROVING, APPROVED, REJECTED)
SUMMARY_CHARS = 300

# Modules whose import registers a kind's handlers. A missing one is skipped (skills/review.py is the capabilities
# worker's, for skill proposals).
HANDLER_MODULES = ('local_agent.memory.review', 'local_agent.skills.review')


class ReviewError(Exception):
    """The item can't be decided now. The message says why, for the owner."""


class NotPending(ReviewError):
    pass


class UnknownKind(ReviewError):
    pass


class NotFound(LookupError):
    pass


@dataclass(frozen=True)
class ReviewItem:
    id: int
    kind: str
    content: dict[str, Any]
    summary: str
    source: str  # what put it here: 'agent', 'distill', 'mcp'...
    run_id: str | None
    taint: list[str]  # the third-party sources the run had read
    status: str
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None  # cli | api
    result: str = ''  # what approving did, or the reason for a rejection


@dataclass(frozen=True)
class ReviewContext:
    home: Any  # config.Home
    store: Any


@dataclass(frozen=True)
class Handler:
    approve: Callable[[ReviewItem, ReviewContext], str]
    reject: Callable[[ReviewItem, ReviewContext], None] | None = None
    show: Callable[[ReviewItem], str] | None = None


_HANDLERS: dict[str, Handler] = {}


def register(kind: str, approve, reject=None, show=None) -> None:
    """What approving (and optionally rejecting, showing) an item of this kind does. The last one registered wins."""
    _HANDLERS[kind] = Handler(approve, reject, show)


def handler_for(kind: str) -> Handler | None:
    for module in HANDLER_MODULES:
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as e:
            if e.name != module:
                raise
    return _HANDLERS.get(kind)


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec='microseconds')


def _redacted(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): _redacted(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redacted(item) for item in value]
    return value


def _item(row: dict) -> ReviewItem:
    return ReviewItem(id=row['id'], kind=row['kind'], content=json.loads(row['content']), summary=row['summary'],
                      source=row['source'], run_id=row['run_id'], taint=json.loads(row['taint']),
                      status=row['status'], created_at=datetime.fromisoformat(row['created_at']),
                      decided_at=datetime.fromisoformat(row['decided_at']) if row['decided_at'] else None,
                      decided_by=row['decided_by'], result=row['result'])


class ReviewQueue(ModuleTables):
    """The queue's rows, on the store."""

    def add(self, kind: str, content: dict[str, Any], summary: str, source: str, run_id: str | None = None,
            taint=(), key: str | None = None) -> ReviewItem:
        """
        Queues an item. With a key, a pending item of the same kind and key is returned instead of a second one
        (the agent saving a fact and distill finding the same fact again).
        """
        if key is not None:
            same = self.query('SELECT * FROM security_review_items WHERE kind = ? AND dedupe_key = ? AND status = ?',
                              (kind, key, PENDING))
            if same:
                return _item(same[0])
        summary = redact_text(summary)
        summary = summary if len(summary) <= SUMMARY_CHARS else summary[:SUMMARY_CHARS - 3] + '...'
        taint = [redact_text(str(source)) for source in taint]
        item_id = self.execute(
            'INSERT INTO security_review_items (kind, content, summary, dedupe_key, source, run_id, taint, status, '
            'created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (kind, json.dumps(_redacted(content)), summary, key, source, run_id, json.dumps(taint), PENDING,
             _stamp(_now()))).lastrowid
        # Never the content: it's third-party text, and the event log is shown to the owner as fact
        ev.record_event(self.store, ev.REVIEW_QUEUED, id=item_id, kind=kind, source=source, run_id=run_id,
                        taint=taint)
        return self.get(item_id)

    def get(self, item_id: int) -> ReviewItem | None:
        rows = self.query('SELECT * FROM security_review_items WHERE id = ?', (item_id,))
        return _item(rows[0]) if rows else None

    def list(self, status: str = PENDING, kind: str | None = None) -> list[ReviewItem]:
        """Items with this status ('all' for every one), oldest first."""
        where, params = [], []
        if status != 'all':
            where.append('status = ?')
            params.append(status)
        if kind:
            where.append('kind = ?')
            params.append(kind)
        sql = 'SELECT * FROM security_review_items' + (f' WHERE {" AND ".join(where)}' if where else '')
        return [_item(row) for row in self.query(sql + ' ORDER BY id', params)]

    def pending_count(self) -> int:
        return self.query('SELECT COUNT(*) AS n FROM security_review_items WHERE status = ?', (PENDING,))[0]['n']

    def _claim(self, item_id: int) -> ReviewItem:
        """Moves a pending item to 'approving', so two deciders (the CLI and the API) can't both apply it."""
        item = self.get(item_id)
        if item is None:
            raise NotFound(f'There is no review item #{item_id}.')
        if self.execute('UPDATE security_review_items SET status = ? WHERE id = ? AND status = ?',
                        (APPROVING, item_id, PENDING)).rowcount != 1:
            raise NotPending(f'Item #{item_id} is {self.get(item_id).status}, not pending.')
        return item

    def _finish(self, item_id: int, status: str, by: str, result: str) -> ReviewItem:
        self.execute('UPDATE security_review_items SET status = ?, decided_at = ?, decided_by = ?, result = ? '
                     'WHERE id = ?', (status, _stamp(_now()), by, redact_text(result), item_id))
        return self.get(item_id)


def approve(store, home, item_id: int, by: str) -> ReviewItem:
    """Applies the item with its kind's handler. A handler that refuses leaves it pending."""
    queue = ReviewQueue(store)
    item = queue._claim(item_id)
    try:
        handler = handler_for(item.kind)
        if handler is None:
            raise UnknownKind(f'Nothing here knows how to apply a {item.kind!r} item.')
        result = handler.approve(item, ReviewContext(home, store))
    except BaseException:
        queue.execute('UPDATE security_review_items SET status = ? WHERE id = ?', (PENDING, item_id))
        raise
    decided = queue._finish(item_id, APPROVED, by, str(result or ''))
    ev.record_event(store, ev.REVIEW_APPROVED, id=item_id, kind=item.kind, by=by, run_id=item.run_id)
    return decided


def reject(store, home, item_id: int, by: str, reason: str = '') -> ReviewItem:
    """Drops the item: nothing of it is applied."""
    queue = ReviewQueue(store)
    item = queue._claim(item_id)
    handler = handler_for(item.kind)
    if handler and handler.reject:
        try:
            handler.reject(item, ReviewContext(home, store))
        except BaseException:
            queue.execute('UPDATE security_review_items SET status = ? WHERE id = ?', (PENDING, item_id))
            raise
    decided = queue._finish(item_id, REJECTED, by, reason)
    ev.record_event(store, ev.REVIEW_REJECTED, id=item_id, kind=item.kind, by=by, run_id=item.run_id)
    return decided


def describe(item: ReviewItem) -> str:
    """The kind's own detail for `review show`, or the content as JSON."""
    handler = handler_for(item.kind)
    if handler and handler.show:
        with contextlib.suppress(Exception):
            return handler.show(item)
    return json.dumps(item.content, indent=2, ensure_ascii=False)


def notification(counts: dict[str, int]) -> str:
    """The owner's message: how many items of each kind, and where to see them. Never their content."""
    total = sum(counts.values())
    kinds = ', '.join(f'{n} {kind.replace("_", " ")}' for kind, n in sorted(counts.items()))
    return (f'{total} item{"s" if total != 1 else ""} wait{"" if total != 1 else "s"} for your review ({kinds}). '
            'Nothing from them is used until you approve: local-agent review list')


async def notify_pending(store, notifier) -> int:
    """One message for every pending item the owner hasn't been told about yet. Returns how many it covered."""
    queue = ReviewQueue(store)
    rows = queue.query('SELECT id, kind FROM security_review_items WHERE status = ? AND notified_at IS NULL',
                       (PENDING,))
    if not rows:
        return 0
    counts: dict[str, int] = {}
    for row in rows:
        counts[row['kind']] = counts.get(row['kind'], 0) + 1
    channel = await notifier.deliver(notification(counts))
    ids = [row['id'] for row in rows]
    queue.execute(f'UPDATE security_review_items SET notified_at = ? WHERE id IN ({",".join("?" * len(ids))})',
                  (_stamp(_now()), *ids))
    ev.record_event(store, ev.REVIEW_NOTIFIED, items=len(ids), counts=counts, channel=str(channel))
    return len(ids)


async def notify_job(rt) -> int:
    """The daemon's periodic job: items queued by distill, another process, or a run without a notifier."""
    return await notify_pending(rt.store, rt.notifier)
