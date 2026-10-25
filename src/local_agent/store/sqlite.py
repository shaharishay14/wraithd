"""
The Store on one SQLite file. Payloads are JSON text, queried with SQLite's json_extract.

Timestamps are stored as UTC ISO strings with a fixed width, so comparing them as text compares them as
times.
"""

import json
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from local_agent.core.events import RUN_END, RUN_START, Event
from local_agent.store.base import APPROVAL_STATUSES, PENDING, Approval

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY,
    run_id TEXT,              -- NULL for events between runs
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_events_run ON run_events (run_id, seq);
CREATE INDEX IF NOT EXISTS run_events_type ON run_events (type, created_at);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
"""


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec='microseconds')


def _parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


class SQLiteStore:
    def __init__(self, path: str | Path = ':memory:'):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection shared by the process, guarded by a lock: the daemon's API, scheduler and runs are
        # all in one process, and approvals are decided from a different task than the one waiting.
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if str(path) != ':memory:':
                self._db.execute('PRAGMA journal_mode=WAL')
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _query(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, parameters).fetchall()

    # --- the event log -----------------------------------------------------------------------------

    def save_events(self, run_id, events) -> None:
        rows = [(run_id, event.seq, event.type, json.dumps(event.payload), _stamp(event.created_at))
                for event in events]
        with self._lock, self._db:
            self._db.executemany(
                'INSERT INTO run_events (run_id, seq, type, payload, created_at) VALUES (?, ?, ?, ?, ?)', rows)

    def run_events(self, run_id) -> list[Event]:
        rows = self._query('SELECT seq, type, payload, created_at FROM run_events WHERE run_id = ? ORDER BY seq',
                           (run_id,))
        return [Event(seq=row['seq'], type=row['type'], payload=json.loads(row['payload']),
                      created_at=_parse(row['created_at'])) for row in rows]

    def latest_run(self, *, source, since, exclude=None):
        rows = self._query(
            "SELECT run_id FROM run_events WHERE type = ? AND json_extract(payload, '$.source') = ? "
            'AND created_at >= ? AND run_id IS NOT NULL AND run_id IS NOT ? ORDER BY created_at DESC LIMIT 1',
            (RUN_START, source, _stamp(since), exclude))
        return rows[0]['run_id'] if rows else None

    def tokens_since(self, since) -> int:
        rows = self._query(
            "SELECT COALESCE(SUM(json_extract(payload, '$.tokens')), 0) AS tokens FROM run_events "
            'WHERE type = ? AND created_at >= ?', (RUN_END, _stamp(since)))
        return int(rows[0]['tokens'])

    # --- approvals ---------------------------------------------------------------------------------

    def create_approval(self, tool, arguments, status=PENDING) -> Approval:
        if status not in APPROVAL_STATUSES:
            raise ValueError(f'Unknown approval status {status!r}')
        now = datetime.now(UTC)
        approval = Approval(id=str(uuid.uuid4()), tool=tool, arguments=arguments, status=status, created_at=now,
                            decided_at=None if status == PENDING else now)
        with self._lock, self._db:
            self._db.execute(
                'INSERT INTO approvals (id, tool, arguments, status, created_at, decided_at) VALUES (?, ?, ?, ?, ?, ?)',
                (approval.id, tool, json.dumps(arguments), status, _stamp(now),
                 approval.decided_at and _stamp(approval.decided_at)))
        return approval

    def get_approval(self, approval_id) -> Approval | None:
        rows = self._query('SELECT * FROM approvals WHERE id = ?', (approval_id,))
        if not rows:
            return None
        row = rows[0]
        return Approval(id=row['id'], tool=row['tool'], arguments=json.loads(row['arguments']), status=row['status'],
                        created_at=_parse(row['created_at']), decided_at=_parse(row['decided_at']))

    def decide_approval(self, approval_id, status) -> bool:
        if status not in APPROVAL_STATUSES or status == PENDING:
            raise ValueError(f'Not a decision: {status!r}')
        with self._lock, self._db:
            # Only a pending approval can be decided: a late click can't overturn an expiry, or the reverse
            cursor = self._db.execute('UPDATE approvals SET status = ?, decided_at = ? WHERE id = ? AND status = ?',
                                      (status, _stamp(datetime.now(UTC)), approval_id, PENDING))
        return cursor.rowcount == 1
