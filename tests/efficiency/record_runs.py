"""
Records E1's fixtures in runs/: real runs whose model reports input tokens as a provider would, saved as event
logs. Run again after a deliberate change to the context, then check the diff:

    RECORD_RUNS=1 uv run pytest tests/efficiency/record_runs.py
"""

import os
from pathlib import Path

import pytest

from local_agent.core import replay
from tests.efficiency import scenarios

pytestmark = pytest.mark.skipif(not os.environ.get('RECORD_RUNS'), reason='only when recording fixtures')
RUNS = Path(__file__).parent / 'runs'


async def test_record_a_tool_run(make_runtime):
    *_, events = await scenarios.a_tool_run(make_runtime)
    replay.dump(events, RUNS / 'tool_run.json')


async def test_record_a_conversation_run(make_runtime):
    *_, events = await scenarios.a_conversation_run(make_runtime)
    replay.dump(events, RUNS / 'conversation_run.json')
