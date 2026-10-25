"""
The Store interface: only the queries the harness actually makes. Each method replaces one ORM query
from agent-studio; there are no users or agents, because one process serves one owner.

Methods are synchronous. A local SQLite write takes well under a millisecond, so async callers call them
directly rather than paying for a thread hop.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
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
    external: str | None = None  # the channel's own id for it, e.g. a Telegram chat id


@dataclass(frozen=True)
class ConversationListing:
    id: str
    source: str
    started_at: datetime
    last_message_at: datetime | None
    preview: str  # the first message, cut short


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    started_at: datetime
    source: str
    message: str


@dataclass(frozen=True)
class Schedule:
    """A standing instruction to run later: on a cron in the owner's timezone, or once at a time."""

    id: int
    message: str  # what the run is asked, as the owner approved it
    cron: str | None  # "0 8 * * 1-5"; None for a one-off
    at: str | None  # ISO local time for a one-off; None for a cron
    timezone: str  # IANA name the cron and the time are read in
    next_fire_at: datetime | None  # UTC; None once a one-off has fired
    created_at: datetime
    last_fired_at: datetime | None = None
    active: bool = True


@dataclass(frozen=True)
class Approval:
    """A write action the agent wanted to run, and what the owner decided."""

    id: str
    tool: str
    arguments: dict[str, Any]
    status: str
    created_at: datetime
    decided_at: datetime | None
    card: dict[str, Any] = field(default_factory=dict)  # what the owner was shown: reason, warnings, channel, run


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

    def start_conversation(self, source: str, external: str | None = None) -> Conversation: ...

    def find_conversation(self, source: str, external: str) -> Conversation | None:
        """The newest conversation of a channel's own chat (e.g. one Telegram chat)."""

    def list_conversations(self, limit: int = 50) -> list[ConversationListing]:
        """Newest activity first."""

    def undistilled_conversations(self) -> list[str]:
        """Conversations with messages distill hasn't read yet."""

    def get_conversation(self, conversation_id: str) -> Conversation | None: ...

    def update_conversation(self, conversation_id: str, **fields) -> None:
        """Sets summary, summarized_until or distilled_until."""

    def add_message(self, conversation_id: str, role: str, content: str) -> Message: ...

    def messages(self, conversation_id: str, after: int = 0) -> list[Message]:
        """The conversation's messages with an id above `after`, oldest first."""

    # --- schedules ---------------------------------------------------------------------------------

    def add_schedule(self, message: str, cron: str | None, at: str | None, timezone: str,
                     next_fire_at: datetime) -> Schedule: ...

    def list_schedules(self, active_only: bool = True) -> list[Schedule]: ...

    def get_schedule(self, schedule_id: int) -> Schedule | None: ...

    def cancel_schedule(self, schedule_id: int) -> bool:
        """Turns a schedule off. False if there was no active schedule with that id."""

    def due_schedules(self, now: datetime) -> list[Schedule]:
        """Active schedules whose next fire time is at or before now."""

    def claim_schedule(self, schedule_id: int, expected_next: datetime, next_fire_at: datetime | None,
                       fired_at: datetime) -> bool:
        """
        Moves a due schedule to its next fire time (None: it's done) only if it still holds expected_next, so two
        callers can't both fire it. True if this caller claimed it.
        """

    # --- approvals ---------------------------------------------------------------------------------

    def create_approval(self, tool: str, arguments: dict[str, Any], status: str = PENDING,
                        card: dict[str, Any] | None = None) -> Approval:
        """Records an approval request (or, with PREAPPROVED, an unattended write that ran)."""

    def list_approvals(self, status: str | None = PENDING, limit: int = 100) -> list[Approval]:
        """Newest first; status None lists every one."""

    def get_approval(self, approval_id: str) -> Approval | None: ...

    def decide_approval(self, approval_id: str, status: str) -> bool:
        """Sets the decision if the approval is still pending. False if it was decided already."""

    # --- settings ----------------------------------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        """Small state the daemon keeps (the paired Telegram chat, a pending pairing code)."""

    def set_setting(self, key: str, value: str | None) -> None:
        """None removes it."""
