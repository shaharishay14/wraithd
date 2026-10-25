"""
Replay: feed a recorded run's events back through today's stop conditions and reminders, without calling a
model, and report every point where today's harness would decide differently.

A recorded run is a regression test for the deterministic part of the harness: change a threshold or a rule,
and the runs whose decisions it changes show up as differences. A real run can be saved with
`dump(store.run_events(run_id), path)`.

Only decisions that follow from the events alone are replayed. Reminders that also look at things the log
doesn't keep (the previous run, the skill list, the owner's message) are skipped.
"""

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from local_agent.core import events as ev
from local_agent.core import reminders
from local_agent.core.stop import StopPolicy

# Reminders whose check reads only the run's events
REPLAYABLE_REMINDERS = {'untrusted_content', 'tool_errors', 'repeated_call', 'context_pressure'}
# Stops decided outside the replayable rules: the SDK's turn limit, the day's total
UNREPLAYABLE_STOPS = {'max_turns', 'daily_budget'}


@dataclass
class RecordedEvent:
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: Any = None


def load(path) -> list[RecordedEvent]:
    data = json.loads(Path(path).read_text())
    return [RecordedEvent(seq=e['seq'], type=e['type'], payload=e['payload']) for e in data['events']]


def dump(events, path) -> None:
    Path(path).write_text(json.dumps({'events': [{'seq': e.seq, 'type': e.type, 'payload': e.payload} for e in events]},
                                     indent=1, default=str) + '\n')


def _situation(prefix, context) -> reminders.Situation:
    return reminders.Situation(message='', events=prefix, owner=context.get('memory_notes') is not None,
                               context_window=reminders.context_window_of(context.get('model')))


def replay(events: list[RecordedEvent], policy: StopPolicy | None = None) -> list[str]:
    """Differences between what was recorded and what today's harness decides. [] means none."""
    policy = replace(policy or StopPolicy.default(), daily_token_budget=None)
    replayable = [r for r in reminders.REMINDERS if r.name in REPLAYABLE_REMINDERS]
    context = next((e.payload for e in events if e.type == ev.CONTEXT_BUILT), {})
    differences = []
    stopped = False  # after a stop, the one remaining model call is the final answer: no stop policy on it

    for index, event in enumerate(events):
        prefix = events[:index]
        # Stops are checked before each model call: a turn start means "don't stop", a stop names the reason
        if event.type == ev.TURN_START and not stopped:
            reason = policy.check(prefix)
            if reason:
                differences.append(f'#{event.seq}: would stop ({reason.code}) before this model call')
        elif event.type == ev.STOP:
            stopped = True
            if event.payload.get('reason') in UNREPLAYABLE_STOPS:
                continue
            reason = policy.check(prefix)
            if not reason or reason.code != event.payload.get('reason'):
                differences.append(f'#{event.seq}: recorded stop {event.payload.get("reason")}, '
                                   f'now {reason.code if reason else "no stop"}')

        # Tool-bound reminders are added right after a result: compare what fired then and now
        if event.type == ev.TOOL_RESULT:
            tool = event.payload.get('tool')
            recorded = []
            for after in events[index + 1:]:
                if after.type != ev.REMINDER or after.payload.get('at') != tool:
                    break
                recorded.append(after.payload.get('name'))
            recorded = [name for name in recorded if name in REPLAYABLE_REMINDERS]
            now = [r.name for r, _ in reminders.due(replayable, tool, _situation(events[:index + 1], context))]
            if sorted(now) != sorted(recorded):
                differences.append(f'#{event.seq} ({tool}): reminders recorded {recorded}, now {now}')
    return differences
