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

    # --- approvals ---------------------------------------------------------------------------------

    def create_approval(self, tool: str, arguments: dict[str, Any], status: str = PENDING) -> Approval:
        """Records an approval request (or, with PREAPPROVED, an unattended write that ran)."""

    def get_approval(self, approval_id: str) -> Approval | None: ...

    def decide_approval(self, approval_id: str, status: str) -> bool:
        """Sets the decision if the approval is still pending. False if it was decided already."""
