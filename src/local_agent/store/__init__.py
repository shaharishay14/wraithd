"""
Where the harness keeps what must outlive a run: the event log, conversations, schedules and
the approval trail.

The harness only talks to the `Store` protocol, so it never knows about SQL. `SQLiteStore` is the one
implementation: a single file in the state folder, stdlib sqlite3, no server.
"""

from local_agent.store.base import (
    APPROVAL_STATUSES,
    Approval,
    Conversation,
    ConversationListing,
    Message,
    RunInfo,
    Schedule,
    Store,
)
from local_agent.store.sqlite import SQLiteStore

__all__ = ['APPROVAL_STATUSES', 'Approval', 'Conversation', 'ConversationListing', 'Message', 'RunInfo', 'SQLiteStore',
           'Schedule', 'Store']
