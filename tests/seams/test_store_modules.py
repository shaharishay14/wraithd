"""A module's own tables: namespaced migrations, applied once, and its own queries."""

import sqlite3

import pytest

from local_agent.store import SQLiteStore
from local_agent.store.modules import MIGRATIONS, Migration, ModuleTables, check

REVIEWS = [
    Migration('security_001', 'CREATE TABLE security_reviews (id INTEGER PRIMARY KEY, text TEXT NOT NULL);'),
    Migration('security_002', "ALTER TABLE security_reviews ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';"),
    Migration('capabilities_001', 'CREATE TABLE IF NOT EXISTS capabilities_runs (child TEXT, parent TEXT);'),
]


class Reviews(ModuleTables):
    def add(self, text):
        return self.execute('INSERT INTO security_reviews (text) VALUES (?)', (text,)).lastrowid

    def pending(self):
        return self.query("SELECT id, text FROM security_reviews WHERE status = 'pending' ORDER BY id")


def applied(store):
    with store.transaction() as db:
        return [row['id'] for row in db.execute('SELECT id FROM schema_migrations ORDER BY id')]


def test_migrations_apply_once_and_are_recorded(tmp_path):
    path = tmp_path / 'state.db'
    store = SQLiteStore(path, migrations=REVIEWS[:1])
    Reviews(store).add('first')
    store.close()

    store = SQLiteStore(path, migrations=REVIEWS)  # reopened with a new one: only that one runs
    assert applied(store) == ['capabilities_001', 'security_001', 'security_002']
    assert Reviews(store).pending() == [{'id': 1, 'text': 'first'}]
    store.close()

    store = SQLiteStore(path, migrations=REVIEWS)  # nothing left to apply (an ALTER twice would fail)
    assert len(applied(store)) == 3
    store.close()


def test_a_failed_migration_leaves_nothing_behind(tmp_path):
    broken = [Migration('evals_001', 'CREATE TABLE evals_runs (id INTEGER); INSERT INTO nowhere VALUES (1);')]
    with pytest.raises(sqlite3.OperationalError):
        SQLiteStore(tmp_path / 'state.db', migrations=broken)
    store = SQLiteStore(tmp_path / 'state.db', migrations=[])
    with store.transaction() as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name = 'evals_runs'").fetchall()
    assert applied(store) == []
    store.close()


@pytest.mark.parametrize('migrations, message', [
    ([Migration('Security-1', 'SELECT 1;')], 'must look like security_001'),
    ([Migration('security_001', 'SELECT 1;')] * 2, 'registered twice'),
    ([Migration('security_001', 'CREATE TABLE reviews (id INTEGER);')], 'must be named security_'),
    ([Migration('reach_001', 'CREATE TABLE IF NOT EXISTS "approvals" (id INTEGER);')], 'must be named reach_'),
])
def test_bad_migrations_are_refused(migrations, message):
    with pytest.raises(ValueError, match=message):
        check(migrations)


def test_registered_migrations_are_valid():
    check(MIGRATIONS)


def test_a_failing_query_rolls_back():
    store = SQLiteStore(':memory:', migrations=REVIEWS)
    reviews = Reviews(store)
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as db:
        db.execute("INSERT INTO security_reviews (text) VALUES ('kept?')")
        db.execute('INSERT INTO security_reviews (text) VALUES (NULL)')
    assert reviews.pending() == []
    store.close()
