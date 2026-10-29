"""
The event log: every step of a run, in order. The single source of truth for what happened.

Events are kept in memory while the run is going (the views read them before every model call) and
written to the store in one batch when the run ends, so logging never slows the loop down.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from local_agent.core.secrets import redact_text

logger = logging.getLogger(__name__)

RUN_START = 'run_start'
CONTEXT_BUILT = 'context_built'  # what the model was given: tools offered, size of the instructions
TURN_START = 'turn_start'  # one model call
MODEL_OUTPUT = 'model_output'  # what that call returned, with token usage
TOOL_CALL = 'tool_call'  # the model asked for a tool; payload says what the gate decided
TOOL_RESULT = 'tool_result'  # what came back: ok / error / denied
STOP = 'stop'  # a stop condition fired
MODEL_UPGRADE = 'model_upgrade'  # a skill asked for the strong model; the rest of the run uses it
REMINDER = 'reminder'  # a system reminder was added (reminders.py)
RUN_END = 'run_end'
COMPACTION = 'compaction'  # older chat messages were folded into the summary (between runs)
DISTILL = 'distill'  # new chat messages were turned into memory notes (between runs)
TELEGRAM_IGNORED = 'telegram_ignored'  # a chat that isn't the owner's wrote to the bot (at most hourly per chat)
# New event types: each worker inside its own block only
# >>> security
REVIEW_QUEUED = 'review_queued'  # something from third-party text waits for the owner (review/queue.py); no content
REVIEW_APPROVED = 'review_approved'  # the owner approved a queued item, and it was applied
REVIEW_REJECTED = 'review_rejected'  # the owner rejected one: nothing of it was applied
REVIEW_NOTIFIED = 'review_notified'  # the owner was told how many items wait (counts only)
MCP_TOOL_CHANGED = 'mcp_tool_changed'  # a tool's definition differs from what the owner accepted (mcp/pinning.py)
MCP_TOOL_NEW = 'mcp_tool_new'  # a tool appeared on a known server: it asks until reviewed
MCP_TOOL_PINNED = 'mcp_tool_pinned'  # always-allow was pinned to a tool's exact definition
# <<< security
# >>> efficiency
CONTEXT_TOKENS = 'context_tokens'  # one model call's input, measured part by part, and the usage reported (tokens.py)
SPILLED = 'spilled'  # a big tool result was saved to workspace/files/.spill/; the model got a preview (spill/)
SPILL_CLEANED = 'spill_cleaned'  # old spill folders were deleted (between runs)
# <<< efficiency
# >>> capabilities
# <<< capabilities
# >>> reach
# <<< reach
# >>> evals
# <<< evals
# >>> sandbox
# <<< sandbox

# Events that belong to no run. A worker adds its own between-runs types inside its block.
BETWEEN_RUNS = (
    COMPACTION, DISTILL, TELEGRAM_IGNORED,
    # >>> security
    REVIEW_QUEUED, REVIEW_APPROVED, REVIEW_REJECTED, REVIEW_NOTIFIED, MCP_TOOL_CHANGED, MCP_TOOL_NEW, MCP_TOOL_PINNED,
    # <<< security
    # >>> efficiency
    SPILL_CLEANED,
    # <<< efficiency
    # >>> capabilities
    # <<< capabilities
    # >>> reach
    # <<< reach
    # >>> evals
    # <<< evals
    # >>> sandbox
    # <<< sandbox
)

# Payload strings longer than this are cut before they're stored (the model got its own rendering)
MAX_PAYLOAD_TEXT = 2000


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Event:
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=_now)


def _storable(value: Any) -> Any:
    """
    JSON-safe copy of a payload: secrets redacted (arguments the model wrote can hold them too), and long
    strings cut so one big tool result can't bloat the table.
    """
    if isinstance(value, str):
        value = redact_text(value)
        return value if len(value) <= MAX_PAYLOAD_TEXT else value[:MAX_PAYLOAD_TEXT] + f'... [{len(value)} chars]'
    if isinstance(value, dict):
        return {str(key): _storable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_storable(item) for item in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return _storable(json.dumps(value, default=str))


class EventLog:
    """The events of one run. Without a store (tests, the forced final answer) it's only kept in memory."""

    def __init__(self, store=None, run_id: str | None = None):
        self.store = store
        self.run_id = run_id or str(uuid.uuid4())
        self.events: list[Event] = []
        self._saved = 0  # how many events are already in the store

    def append(self, event_type: str, **payload) -> Event:
        event = Event(seq=len(self.events), type=event_type, payload=_storable(payload))
        self.events.append(event)
        return event

    def of_type(self, *types: str) -> list[Event]:
        return [event for event in self.events if event.type in types]

    def save(self) -> None:
        """Writes the events not saved yet. A failure is logged, never raised: the owner's answer matters more."""
        pending = self.events[self._saved:]
        if not pending or self.store is None:
            return
        try:
            self.store.save_events(self.run_id, pending)
            self._saved = len(self.events)
        except Exception as e:
            logger.warning(f'Could not save {len(pending)} run events for run {self.run_id}: {type(e).__name__}: {e}')


def record_event(store, event_type: str, **payload) -> None:
    """Saves a single event that belongs to no run (like a compaction between two runs)."""
    try:
        store.save_events(None, [Event(seq=0, type=event_type, payload=_storable(payload))])
    except Exception as e:
        logger.warning(f'Could not save {event_type} event: {type(e).__name__}: {e}')
