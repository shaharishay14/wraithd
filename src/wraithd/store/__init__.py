"""
Where the harness keeps what must outlive a run: the event log, conversations, schedules and
the approval trail.

The harness only talks to the `Store` protocol, so it never knows about SQL. `SQLiteStore` is the one
implementation: a single file in the state folder, stdlib sqlite3, no server.
"""

from wraithd.store.base import (
    APPROVAL_STATUSES,
    Approval,
    Conversation,
    ConversationListing,
    Message,
    RunInfo,
    Schedule,
    Store,
)
from wraithd.store.sqlite import SQLiteStore

__all__ = ['APPROVAL_STATUSES', 'Approval', 'Conversation', 'ConversationListing', 'Message', 'RunInfo', 'SQLiteStore',
           'Schedule', 'Store']
