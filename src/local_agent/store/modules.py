"""
Tables a module owns, without touching the core schema (sqlite.py) or the Store interface (base.py).

A module registers its migrations inside its own block of MIGRATIONS: ids are `<module>_NNN` (security_001,
capabilities_001), and every table one creates is named `<module>_...`. The store applies the ones it hasn't yet,
in order, each once, and records them in schema_migrations. Never edit a migration that shipped: add the next one.

Its queries live in its own code, on a ModuleTables over the store:

    class Reviews(ModuleTables):
        def add(self, text):
            return self.execute('INSERT INTO security_reviews (text) VALUES (?)', (text,)).lastrowid
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

ID = re.compile(r'^([a-z]+)_(\d{3})$')
CREATE_TABLE = re.compile(r'CREATE\s+(?:TEMP\w*\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`\[]?(\w+)', re.IGNORECASE)


@dataclass(frozen=True)
class Migration:
    id: str  # <module>_NNN
    sql: str  # one or more statements

    @property
    def namespace(self) -> str:
        return ID.match(self.id).group(1)


# Each worker inside its block only, in the order they apply
MIGRATIONS: list[Migration] = [
    # >>> security
    # <<< security
    # >>> efficiency
    # <<< efficiency
    # >>> capabilities
    # <<< capabilities
    # >>> reach
    # <<< reach
    # >>> evals
    # <<< evals
    # >>> sandbox
    # <<< sandbox
]


def check(migrations: Sequence[Migration]) -> None:
    """Raises ValueError for a bad id, a repeated id, or a table outside the migration's namespace."""
    seen = set()
    for migration in migrations:
        if not ID.match(migration.id):
            raise ValueError(f'Migration id {migration.id!r} must look like security_001')
        if migration.id in seen:
            raise ValueError(f'Migration {migration.id} is registered twice')
        seen.add(migration.id)
        for table in CREATE_TABLE.findall(migration.sql):
            if not table.startswith(f'{migration.namespace}_'):
                raise ValueError(f'Migration {migration.id} creates table {table!r}: its tables must be named '
                                 f'{migration.namespace}_...')


class ModuleTables:
    """A module's own queries, on the store's connection: under its lock, each call committed on success."""

    def __init__(self, store):
        self.store = store

    def execute(self, sql: str, params: Sequence[Any] = ()):
        """One statement; returns the cursor (lastrowid, rowcount)."""
        with self.store.transaction() as db:
            return db.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Rows as dicts."""
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute(sql, params)]
