"""
Tool output has two readers: the model, which needs a short, bounded text, and the event log (for the run
inspector and debugging), which needs structured facts about the result.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Max characters of a single tool result that go into the model's context (~1k tokens)
MAX_TOOL_RESULT_CHARS = 4000

# Characters of a result kept in the event log, enough to see what came back
LOG_PREVIEW_CHARS = 500


def cap_tool_result(content: str, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """
    Limits a tool result before it goes into the model's context.
    Keeps the start and tells the model it was cut, so it can ask for less.
    """
    if len(content) <= max_chars:
        return content

    logger.warning(f'Tool result truncated from {len(content)} to {max_chars} chars')
    return (
        content[:max_chars]
        + f'\n\n[TRUNCATED: showing first {max_chars} of {len(content)} characters. '
        'If you need more, call the tool again with a narrower query or fewer results.]'
    )


def outcome_of(text: str) -> str:
    """'error' if a tool result reports a failure ({"error": ...} or {"successful": false}), else 'ok'."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return 'ok'
    if isinstance(data, dict) and (data.get('error') or data.get('successful') is False):
        return 'error'
    return 'ok'


def reports_untrusted(text: str) -> bool:
    """True if a tool result says it holds third-party data ({"untrusted": true}), like a low-trust memory note."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and data.get('untrusted') is True


def for_log(tool_name: str, text: str, outcome: str, untrusted: bool) -> dict[str, Any]:
    """The structured rendering of a tool result, stored as the tool_result event's payload."""
    return {
        'tool': tool_name,
        'outcome': outcome,
        'chars': len(text),
        'truncated': len(text) > MAX_TOOL_RESULT_CHARS,
        'untrusted': untrusted,
        'preview': text[:LOG_PREVIEW_CHARS],
    }
