"""
The runs E1's breakdown is checked on: real run()s through the real SDK loop, a model that reports usage the way a
provider does, and an owner with standing orders, a profile and skills. record_runs.py saves them as fixtures.
"""

from local_agent.memory.notes import Note  # noqa: F401 (tests use it)
from local_agent.memory.vault import PROFILE_PATH
from tests.efficiency.fakes import CountingModel
from tests.scripted import call, fake_tool, run_once, say

AGENTS_MD = '# Standing orders\n\nAnswer in short bullet points. Never book anything without asking me first.\n'
PROFILE = 'Dana, product manager in Lisbon. Prefers metric units and short replies. Vegetarian.'
PAGE = 'Lisbon weather report. ' + 'Sunny spells, a light breeze from the north west, 22C at noon. ' * 30


def owner_setup(rt):
    rt.home.agents_md.write_text(AGENTS_MD)
    rt.memory.save(Note(path=PROFILE_PATH, title='Profile', body=PROFILE))


async def a_tool_run(make_runtime, **model_args):
    """Two model calls: one tool call with a long result, then the answer."""
    model = CountingModel([[call('1', 'get_weather', {'location': 'Lisbon'})], [say('Sunny, 22C.')]], **model_args)
    rt = make_runtime(model)
    owner_setup(rt)
    result, events = await run_once(rt, 'What is the weather in Lisbon today?',
                                    tools=[fake_tool('get_weather', lambda **a: {'report': PAGE[:3000]})])
    return model, rt, result, events


async def a_conversation_run(make_runtime):
    """A run with history: the earlier turns count as history."""
    model = CountingModel([[say('You asked about Lisbon.')]])
    rt = make_runtime(model)
    owner_setup(rt)
    history = [{'role': 'user', 'content': 'What is the weather in Lisbon today?'},
               {'role': 'assistant', 'content': 'Sunny, 22C. A light breeze from the north west.'}]
    result, events = await run_once(rt, 'What did I ask you about?', history=history)
    return model, rt, result, events
