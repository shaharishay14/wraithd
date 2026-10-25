"""
State of a run, derived from its events. Plain functions over a list of events: nothing to keep in sync,
they can be recomputed at any time (live, before every model call, or later from the store).
"""

import json
from collections import Counter
from collections.abc import Iterable

from local_agent.core import events as ev

SKILL_LOAD = 'skill_load'


def _of_type(events: Iterable, event_type: str) -> list:
    return [event for event in events if event.type == event_type]


def turns(events) -> int:
    """How many model calls the run has made."""
    return len(_of_type(events, ev.TURN_START))


def tokens_used(events) -> int:
    """Input plus output tokens of every model call so far."""
    return sum(event.payload.get('input_tokens', 0) + event.payload.get('output_tokens', 0)
               for event in _of_type(events, ev.MODEL_OUTPUT))


def consecutive_tool_errors(events) -> int:
    """Tool results that failed in a row, counting back from the latest. Any success resets it."""
    count = 0
    for event in reversed(_of_type(events, ev.TOOL_RESULT)):
        if event.payload.get('outcome') != 'error':
            break
        count += 1
    return count


def _call_key(event) -> str:
    return event.payload.get('tool', '') + ' ' + json.dumps(event.payload.get('arguments', {}), sort_keys=True)


def most_repeated_call(events):
    """(tool, times) for the tool call made most often with the exact same arguments, or (None, 0)."""
    counts = Counter(_call_key(event) for event in _of_type(events, ev.TOOL_CALL))
    if not counts:
        return None, 0
    key, times = counts.most_common(1)[0]
    return key.split(' ', 1)[0], times


def tools_called(events) -> list[str]:
    """Every tool the model asked for, in order (including ones the gate blocked)."""
    return [event.payload.get('tool') for event in _of_type(events, ev.TOOL_CALL)]


def untrusted_content_seen(events) -> list[str]:
    """
    Third-party content in the model's context, in the order it arrived: the run's own input (the event that
    started it), then tools whose output was third-party.
    """
    sources = [source for event in _of_type(events, ev.RUN_START)
               for source in event.payload.get('untrusted_inputs', [])]
    for event in _of_type(events, ev.TOOL_RESULT):
        tool = event.payload.get('tool')
        if event.payload.get('untrusted') and tool not in sources:
            sources.append(tool)
    return sources


def skills_loaded(events) -> list[str]:
    """Skills the model loaded in this run, in order (only loads the gate allowed)."""
    return [event.payload.get('arguments', {}).get('name') for event in _of_type(events, ev.TOOL_CALL)
            if event.payload.get('tool') == SKILL_LOAD and event.payload.get('allowed')]


def stop_reason(events):
    """The code of the stop condition that ended the run, or None if the run ended on its own."""
    stops = _of_type(events, ev.STOP)
    return stops[-1].payload.get('reason') if stops else None
