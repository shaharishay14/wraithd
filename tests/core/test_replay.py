"""
Replay: recorded runs (runs/*.json, recorded in agent-studio by real runs of the same harness) go back
through today's stop conditions and reminders with no model. They must decide the same; a rule change that
alters them shows up here.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from local_agent.core import events as ev
from local_agent.core import reminders, replay
from local_agent.core.stop import StopPolicy

RUNS = sorted((Path(__file__).parent / 'runs').glob('*.json'))


def test_there_are_recorded_runs():
    assert {path.stem for path in RUNS} >= {'clean_run', 'tool_errors_stop', 'loop_stop', 'third_party_content'}


@pytest.mark.parametrize('path', RUNS, ids=lambda path: path.stem)
def test_todays_harness_decides_like_the_recorded_run(path):
    assert replay.replay(replay.load(path)) == []


def test_a_looser_stop_rule_shows_up_as_a_difference():
    events = replay.load(Path(__file__).parent / 'runs' / 'tool_errors_stop.json')
    looser = replace(StopPolicy.default(), max_consecutive_errors=5)
    assert replay.replay(events, looser) == ['#15: recorded stop tool_errors, now no stop']


def test_a_stricter_stop_rule_shows_up_too():
    events = replay.load(Path(__file__).parent / 'runs' / 'loop_stop.json')
    stricter = replace(StopPolicy.default(), max_identical_calls=2)
    assert any('would stop (repeated_call)' in difference for difference in replay.replay(events, stricter))


def test_a_reminder_that_stops_firing_shows_up(monkeypatch):
    events = replay.load(Path(__file__).parent / 'runs' / 'third_party_content.json')
    monkeypatch.setattr(reminders, 'REMINDERS', [r for r in reminders.REMINDERS if r.name != 'untrusted_content'])
    differences = replay.replay(events)
    assert len(differences) == 1 and "recorded ['untrusted_content'], now []" in differences[0]


def test_a_run_saved_from_the_store_replays_like_a_fixture(store, tmp_path):
    events = replay.load(Path(__file__).parent / 'runs' / 'loop_stop.json')
    store.save_events('r1', [ev.Event(seq=e.seq, type=e.type, payload=e.payload) for e in events])
    replay.dump(store.run_events('r1'), tmp_path / 'saved.json')
    assert replay.load(tmp_path / 'saved.json') == events
    assert replay.replay(replay.load(tmp_path / 'saved.json')) == []
