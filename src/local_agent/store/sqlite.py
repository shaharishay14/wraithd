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
from local_agent.store.base import (
    APPROVAL_STATUSES,
    PENDING,
    Approval,
    Conversation,
    ConversationListing,
    Message,
    RunInfo,
    Schedule,
)

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

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    summarized_until INTEGER NOT NULL DEFAULT 0,
    distilled_until INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations (id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages (conversation_id, id);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL,
    cron TEXT,
    at TEXT,
    timezone TEXT NOT NULL,
    next_fire_at TEXT,
    created_at TEXT NOT NULL,
    last_fired_at TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS schedules_due ON schedules (active, next_fire_at);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after a table first shipped: (table, column, definition). Added on open when missing, so an older
# state.db keeps working.
MIGRATIONS = [
    ('approvals', 'card', "TEXT NOT NULL DEFAULT '{}'"),
    ('conversations', 'external', 'TEXT'),
]


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
            for table, column, definition in MIGRATIONS:
                columns = {row['name'] for row in self._db.execute(f'PRAGMA table_info({table})')}
                if column not in columns:
                    self._db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
            self._db.commit()

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

    def recent_runs(self, limit=20) -> list[RunInfo]:
        rows = self._query(
            "SELECT run_id, created_at, json_extract(payload, '$.source') AS source, "
            "json_extract(payload, '$.message') AS message FROM run_events "
            'WHERE type = ? AND run_id IS NOT NULL ORDER BY created_at DESC LIMIT ?', (RUN_START, limit))
        return [RunInfo(run_id=row['run_id'], started_at=_parse(row['created_at']), source=row['source'] or '',
                        message=row['message'] or '') for row in rows]

    def find_runs(self, prefix) -> list[str]:
        # LIKE treats % and _ as wildcards, so escape them: a prefix is matched as written
        pattern = prefix.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        rows = self._query("SELECT DISTINCT run_id FROM run_events WHERE run_id LIKE ? ESCAPE '\\' "
                           'AND type = ?', (pattern, RUN_START))
        return sorted(row['run_id'] for row in rows)

    # --- conversations -----------------------------------------------------------------------------

    def start_conversation(self, source, external=None) -> Conversation:
        conversation = Conversation(id=str(uuid.uuid4()), source=source, started_at=datetime.now(UTC),
                                    external=external)
        with self._lock, self._db:
            self._db.execute('INSERT INTO conversations (id, source, started_at, external) VALUES (?, ?, ?, ?)',
                             (conversation.id, source, _stamp(conversation.started_at), external))
        return conversation

    @staticmethod
    def _conversation(row) -> Conversation:
        return Conversation(id=row['id'], source=row['source'], started_at=_parse(row['started_at']),
                            summary=row['summary'], summarized_until=row['summarized_until'],
                            distilled_until=row['distilled_until'], external=row['external'])

    def get_conversation(self, conversation_id) -> Conversation | None:
        rows = self._query('SELECT * FROM conversations WHERE id = ?', (conversation_id,))
        return self._conversation(rows[0]) if rows else None

    def find_conversation(self, source, external) -> Conversation | None:
        rows = self._query('SELECT * FROM conversations WHERE source = ? AND external = ? '
                           'ORDER BY started_at DESC LIMIT 1', (source, external))
        return self._conversation(rows[0]) if rows else None

    def list_conversations(self, limit=50) -> list[ConversationListing]:
        rows = self._query(
            'SELECT c.id, c.source, c.started_at, MAX(m.created_at) AS last_message_at, '
            '(SELECT content FROM messages WHERE conversation_id = c.id ORDER BY id LIMIT 1) AS preview '
            'FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id '
            'GROUP BY c.id ORDER BY COALESCE(MAX(m.created_at), c.started_at) DESC LIMIT ?', (limit,))
        return [ConversationListing(id=row['id'], source=row['source'], started_at=_parse(row['started_at']),
                                    last_message_at=_parse(row['last_message_at']),
                                    preview=(row['preview'] or '')[:120]) for row in rows]

    def undistilled_conversations(self) -> list[str]:
        rows = self._query('SELECT DISTINCT c.id FROM conversations c JOIN messages m ON m.conversation_id = c.id '
                           'WHERE m.id > c.distilled_until ORDER BY c.id')
        return [row['id'] for row in rows]

    def update_conversation(self, conversation_id, **fields) -> None:
        allowed = {'summary', 'summarized_until', 'distilled_until'}
        if not fields or set(fields) - allowed:
            raise ValueError(f'Only {", ".join(sorted(allowed))} can be updated.')
        assignments = ', '.join(f'{name} = ?' for name in fields)  # names checked above, never from input
        with self._lock, self._db:
            self._db.execute(f'UPDATE conversations SET {assignments} WHERE id = ?',
                             (*fields.values(), conversation_id))

    def add_message(self, conversation_id, role, content) -> Message:
        now = datetime.now(UTC)
        with self._lock, self._db:
            cursor = self._db.execute(
                'INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)',
                (conversation_id, role, content, _stamp(now)))
        return Message(id=cursor.lastrowid, role=role, content=content, created_at=now)

    def messages(self, conversation_id, after=0) -> list[Message]:
        rows = self._query('SELECT * FROM messages WHERE conversation_id = ? AND id > ? ORDER BY id',
                           (conversation_id, after))
        return [Message(id=row['id'], role=row['role'], content=row['content'], created_at=_parse(row['created_at']))
                for row in rows]

    # --- schedules ---------------------------------------------------------------------------------

    @staticmethod
    def _schedule(row) -> Schedule:
        return Schedule(id=row['id'], message=row['message'], cron=row['cron'], at=row['at'],
                        timezone=row['timezone'], next_fire_at=_parse(row['next_fire_at']),
                        created_at=_parse(row['created_at']), last_fired_at=_parse(row['last_fired_at']),
                        active=bool(row['active']))

    def add_schedule(self, message, cron, at, timezone, next_fire_at) -> Schedule:
        now = datetime.now(UTC)
        with self._lock, self._db:
            cursor = self._db.execute(
                'INSERT INTO schedules (message, cron, at, timezone, next_fire_at, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (message, cron, at, timezone, _stamp(next_fire_at), _stamp(now)))
        return self.get_schedule(cursor.lastrowid)

    def list_schedules(self, active_only=True) -> list[Schedule]:
        where = 'WHERE active = 1 ' if active_only else ''
        return [self._schedule(row) for row in self._query(f'SELECT * FROM schedules {where}ORDER BY id')]

    def get_schedule(self, schedule_id) -> Schedule | None:
        rows = self._query('SELECT * FROM schedules WHERE id = ?', (schedule_id,))
        return self._schedule(rows[0]) if rows else None

    def cancel_schedule(self, schedule_id) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute('UPDATE schedules SET active = 0 WHERE id = ? AND active = 1', (schedule_id,))
        return cursor.rowcount == 1

    def due_schedules(self, now) -> list[Schedule]:
        rows = self._query('SELECT * FROM schedules WHERE active = 1 AND next_fire_at IS NOT NULL '
                           'AND next_fire_at <= ? ORDER BY next_fire_at, id', (_stamp(now),))
        return [self._schedule(row) for row in rows]

    def claim_schedule(self, schedule_id, expected_next, next_fire_at, fired_at) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                'UPDATE schedules SET next_fire_at = ?, last_fired_at = ?, active = ? '
                'WHERE id = ? AND active = 1 AND next_fire_at = ?',
                (next_fire_at and _stamp(next_fire_at), _stamp(fired_at), int(next_fire_at is not None),
                 schedule_id, _stamp(expected_next)))
        return cursor.rowcount == 1

    # --- approvals ---------------------------------------------------------------------------------

    def create_approval(self, tool, arguments, status=PENDING, card=None) -> Approval:
        if status not in APPROVAL_STATUSES:
            raise ValueError(f'Unknown approval status {status!r}')
        now = datetime.now(UTC)
        approval = Approval(id=str(uuid.uuid4()), tool=tool, arguments=arguments, status=status, created_at=now,
                            decided_at=None if status == PENDING else now, card=dict(card or {}))
        with self._lock, self._db:
            self._db.execute(
                'INSERT INTO approvals (id, tool, arguments, status, created_at, decided_at, card) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (approval.id, tool, json.dumps(arguments), status, _stamp(now),
                 approval.decided_at and _stamp(approval.decided_at), json.dumps(approval.card)))
        return approval

    @staticmethod
    def _approval(row) -> Approval:
        return Approval(id=row['id'], tool=row['tool'], arguments=json.loads(row['arguments']), status=row['status'],
                        created_at=_parse(row['created_at']), decided_at=_parse(row['decided_at']),
                        card=json.loads(row['card'] or '{}'))

    def get_approval(self, approval_id) -> Approval | None:
        rows = self._query('SELECT * FROM approvals WHERE id = ?', (approval_id,))
        return self._approval(rows[0]) if rows else None

    def list_approvals(self, status=PENDING, limit=100) -> list[Approval]:
        where, parameters = ('WHERE status = ? ', (status, limit)) if status else ('', (limit,))
        rows = self._query(f'SELECT * FROM approvals {where}ORDER BY created_at DESC LIMIT ?', parameters)
        return [self._approval(row) for row in rows]

    def decide_approval(self, approval_id, status) -> bool:
        if status not in APPROVAL_STATUSES or status == PENDING:
            raise ValueError(f'Not a decision: {status!r}')
        with self._lock, self._db:
            # Only a pending approval can be decided: a late click can't overturn an expiry, or the reverse
            cursor = self._db.execute('UPDATE approvals SET status = ?, decided_at = ? WHERE id = ? AND status = ?',
                                      (status, _stamp(datetime.now(UTC)), approval_id, PENDING))
        return cursor.rowcount == 1

    # --- settings ----------------------------------------------------------------------------------

    def get_setting(self, key) -> str | None:
        rows = self._query('SELECT value FROM settings WHERE key = ?', (key,))
        return rows[0]['value'] if rows else None

    def set_setting(self, key, value) -> None:
        with self._lock, self._db:
            if value is None:
                self._db.execute('DELETE FROM settings WHERE key = ?', (key,))
            else:
                self._db.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                                 'ON CONFLICT (key) DO UPDATE SET value = excluded.value', (key, value))
