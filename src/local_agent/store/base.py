"""
The Store interface: only the queries the harness actually makes. Each method replaces one ORM query
from agent-studio; there are no users or agents, because one process serves one owner.

Methods are synchronous. A local SQLite write takes well under a millisecond, so async callers call them
directly rather than paying for a thread hop.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

PENDING = 'pending'
APPROVED = 'approved'
DENIED = 'denied'
EXPIRED = 'expired'
PREAPPROVED = 'preapproved'  # ran unattended because the owner allow-listed the tool
APPROVAL_STATUSES = (PENDING, APPROVED, DENIED, EXPIRED, PREAPPROVED)


@dataclass(frozen=True)
class Message:
    id: int  # increases with every message, so it doubles as a cursor ("everything after message 41")
    role: str  # user | assistant
    content: str
    created_at: datetime


@dataclass(frozen=True)
class Conversation:
    """One conversation (a terminal session, later a Telegram chat), and how far compaction and distill got."""

    id: str
    source: str
    started_at: datetime
    summary: str = ''  # older messages, folded into a rolling summary (compaction.py)
    summarized_until: int = 0  # id of the newest message in the summary
    distilled_until: int = 0  # id of the newest message distill has read (memory/distill.py)


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    started_at: datetime
    source: str
    message: str


@dataclass(frozen=True)
class Approval:
    """A write action the agent wanted to run, and what the owner decided."""

    id: str
    tool: str
    arguments: dict[str, Any]
    status: str
    created_at: datetime
    decided_at: datetime | None


class Store(Protocol):
    # --- the event log -----------------------------------------------------------------------------

    def save_events(self, run_id: str | None, events: Sequence[Any]) -> None:
        """Appends events (anything with seq, type, payload, created_at). run_id None: between runs."""

    def run_events(self, run_id: str) -> list[Any]:
        """One run's events, in order."""

    def latest_run(self, *, source: str, since: datetime, exclude: str | None = None) -> str | None:
        """The id of the newest run with this source that started after `since`, other than `exclude`."""

    def tokens_since(self, since: datetime) -> int:
        """Tokens used by runs that ended after `since` (from their run_end events)."""

    def recent_runs(self, limit: int = 20) -> list[RunInfo]:
        """The newest runs first, from their run_start events."""

    def find_runs(self, prefix: str) -> list[str]:
        """Run ids that start with `prefix` (so the CLI can take a short id)."""

    # --- conversations -----------------------------------------------------------------------------

    def start_conversation(self, source: str) -> Conversation: ...

    def get_conversation(self, conversation_id: str) -> Conversation | None: ...

    def update_conversation(self, conversation_id: str, **fields) -> None:
        """Sets summary, summarized_until or distilled_until."""

    def add_message(self, conversation_id: str, role: str, content: str) -> Message: ...

    def messages(self, conversation_id: str, after: int = 0) -> list[Message]:
        """The conversation's messages with an id above `after`, oldest first."""

    # --- approvals ---------------------------------------------------------------------------------

    def create_approval(self, tool: str, arguments: dict[str, Any], status: str = PENDING) -> Approval:
        """Records an approval request (or, with PREAPPROVED, an unattended write that ran)."""

    def get_approval(self, approval_id: str) -> Approval | None: ...

    def decide_approval(self, approval_id: str, status: str) -> bool:
        """Sets the decision if the approval is still pending. False if it was decided already."""
