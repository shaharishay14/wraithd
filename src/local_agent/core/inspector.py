"""
What the run inspector shows: a run's events, and what they add up to. Everything here is derived from the
event log (views.py), so `local-agent runs show` (and later the web UI) shows exactly what the harness saw and
decided.
"""

import json

from local_agent.core import events as ev
from local_agent.core import views


def _first(events, event_type):
    return next((e for e in events if e.type == event_type), None)


def disclosure_depth(events) -> dict[str, object]:
    """How far the model went down each registry: what it was given, and what it pulled in on demand."""
    called = views.tools_called(events)
    context = _first(events, ev.CONTEXT_BUILT)
    payload = context.payload if context else {}
    memory = ('L3 read' if 'memory_read' in called else 'L2 search' if 'memory_search' in called
              else 'L0-L1 prompt' if payload.get('memory_notes') is not None else None)
    apps = ('run' if 'run_app_action' in called or any(t and t.isupper() for t in called)
            else 'describe' if 'describe_app_action' in called else 'find' if 'find_app_actions' in called
            else None)
    return {
        'mode': payload.get('disclosure'),
        'apps': apps,
        'memory': memory,
        'skills_loaded': [e.payload.get('arguments', {}).get('name') for e in events
                          if e.type == ev.TOOL_CALL and e.payload.get('tool') == 'skill_load'],
        'web_pages_read': called.count('read_website'),
    }


def summarize(events) -> dict[str, object]:
    """One run in numbers."""
    start, end = _first(events, ev.RUN_START), _first(events, ev.RUN_END)
    context = _first(events, ev.CONTEXT_BUILT)
    tool_results = [e for e in events if e.type == ev.TOOL_RESULT]
    return {
        'source': start.payload.get('source') if start else None,
        'message': start.payload.get('message') if start else '',
        'started_at': start.created_at if start else None,
        'outcome': end.payload.get('outcome') if end else 'running',
        'duration_ms': end.payload.get('duration_ms') if end else None,
        'turns': views.turns(events),
        'tokens': views.tokens_used(events),
        'tool_calls': len(views.tools_called(events)),
        'tool_errors': sum(1 for e in tool_results if e.payload.get('outcome') == 'error'),
        'denied': sum(1 for e in tool_results if e.payload.get('outcome') == 'denied'),
        'stop_reason': views.stop_reason(events),
        'untrusted_sources': views.untrusted_content_seen(events),
        'reminders': [e.payload.get('name') for e in events if e.type == ev.REMINDER],
        'redactions': sorted({kind for e in tool_results for kind in e.payload.get('redacted', [])}),
        'tools_offered': len(context.payload.get('tools_offered', [])) if context else 0,
        'tool_schema_chars': context.payload.get('tool_schema_chars') if context else None,
        'model': context.payload.get('model') if context else None,
        'model_upgrades': [e.payload.get('model') for e in events if e.type == ev.MODEL_UPGRADE],
        'instructions_chars': context.payload.get('instructions_chars') if context else None,
        'disclosure': disclosure_depth(events),
    }


def timeline(events) -> list[dict[str, object]]:
    return [{'seq': e.seq, 'type': e.type, 'at': e.created_at, 'payload': e.payload} for e in events]


# --- as text, for the terminal -----------------------------------------------------------------------------

# The payload fields worth a glance per event type; the rest is in --json
HEADLINE = {
    ev.RUN_START: ('source', 'message'),
    ev.CONTEXT_BUILT: ('model', 'disclosure', 'instructions_chars'),
    ev.TURN_START: ('turn',),
    ev.MODEL_OUTPUT: ('input_tokens', 'output_tokens', 'tool_calls'),
    ev.TOOL_CALL: ('tool', 'access', 'allowed', 'arguments'),
    ev.TOOL_RESULT: ('tool', 'outcome', 'chars', 'untrusted', 'redacted'),
    ev.REMINDER: ('name', 'at'),
    ev.STOP: ('reason',),
    ev.MODEL_UPGRADE: ('skill', 'model'),
    ev.RUN_END: ('outcome', 'turns', 'tokens', 'duration_ms', 'stop_reason'),
}
VALUE_CHARS = 80


def _short(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= VALUE_CHARS else text[:VALUE_CHARS - 3] + '...'


def render_text(run_id: str, events) -> str:
    """A run as the terminal shows it: the numbers, then one line per event."""
    summary = summarize(events)
    lines = [
        f'Run {run_id}',
        f'  {summary["outcome"]}' + (f' (stopped: {summary["stop_reason"]})' if summary['stop_reason'] else '')
        + f' · {summary["turns"]} model calls · {summary["tool_calls"]} tool calls · {summary["tokens"]} tokens'
        + (f' · {summary["duration_ms"]} ms' if summary['duration_ms'] is not None else ''),
        f'  message: {_short(summary["message"] or "")}',
    ]
    if summary['untrusted_sources']:
        lines.append(f'  third-party content: {", ".join(summary["untrusted_sources"])}')
    if summary['redactions']:
        lines.append(f'  secrets removed: {", ".join(summary["redactions"])}')
    if summary['reminders']:
        lines.append(f'  reminders: {", ".join(summary["reminders"])}')
    lines.append('')
    for event in events:
        shown = [f'{key}={_short(event.payload[key])}' for key in HEADLINE.get(event.type, ())
                 if event.payload.get(key) not in (None, '', [], {})]
        lines.append(f'  #{event.seq:<3} {event.type:<14} {" ".join(shown)}'.rstrip())
    return '\n'.join(lines)
