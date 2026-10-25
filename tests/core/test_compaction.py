"""
Compaction: recent messages word for word, older ones folded into a rolling summary in batches, stored with the
conversation so a restart doesn't lose it.
"""

from local_agent.core import compaction
from local_agent.core import events as ev
from tests.scripted import ScriptedModel, say


def conversation_with(rt, count):
    conversation = rt.store.start_conversation('terminal')
    for i in range(count):
        rt.store.add_message(conversation.id, 'user' if i % 2 == 0 else 'assistant', f'm{i}')
    return conversation


async def test_compaction_summarizes_in_batches_and_rolls_forward(make_runtime):
    model = ScriptedModel([[say('SUM1')], [say('SUM2')]])
    rt = make_runtime(model)
    conversation = rt.store.start_conversation('terminal')

    def add(count):
        start = len(rt.store.messages(conversation.id))
        for i in range(start, start + count):
            rt.store.add_message(conversation.id, 'user', f'm{i}')

    add(5)
    few = await compaction.history_for(rt.store, conversation.id, rt.complete, exclude_latest=False)
    assert [m['content'] for m in few] == [f'm{i}' for i in range(5)] and not model.inputs

    add(15)  # 20 messages: 10 older (a batch) and the 10 most recent
    first = await compaction.history_for(rt.store, conversation.id, rt.complete, exclude_latest=False)
    assert first[0]['role'] == 'user' and 'SUM1' in first[0]['content']
    assert [m['content'] for m in first[1:]] == [f'm{i}' for i in range(10, 20)]

    add(4)  # 4 more older messages: not a batch yet, so they go as they are
    later = await compaction.history_for(rt.store, conversation.id, rt.complete, exclude_latest=False)
    assert 'SUM1' in later[0]['content'] and [m['content'] for m in later[1:5]] == ['m10', 'm11', 'm12', 'm13']

    add(6)
    rolled = await compaction.history_for(rt.store, conversation.id, rt.complete, exclude_latest=False)
    assert 'SUM1' in model.inputs[1][0]['content']  # the old summary fed into the new one
    assert 'SUM2' in rolled[0]['content'] and len(rolled) == 11


async def test_the_summary_is_stored_and_survives_a_restart(make_runtime):
    rt = make_runtime(ScriptedModel([[say('SUM1')]]))
    conversation = conversation_with(rt, 21)
    await compaction.history_for(rt.store, conversation.id, rt.complete)  # the newest message is this run's own

    # a new process gets the same summary, with no new model call
    again = make_runtime(ScriptedModel([]))
    compacted = await compaction.history_for(again.store, conversation.id, again.complete)
    assert 'SUM1' in compacted[0]['content'] and compaction.is_summarized(compacted)
    stored = again.store.get_conversation(conversation.id)
    assert stored.summary == 'SUM1' and stored.summarized_until == again.store.messages(conversation.id)[9].id
    [row] = again.store._query('SELECT run_id, payload FROM run_events WHERE type = ?', (ev.COMPACTION,))
    assert row['run_id'] is None and '"messages_folded": 10' in row['payload']


async def test_compaction_failure_keeps_messages(make_runtime):
    rt = make_runtime(ScriptedModel([RuntimeError('model down')]))
    conversation = conversation_with(rt, 21)
    result = await compaction.history_for(rt.store, conversation.id, rt.complete)
    assert len(result) == 20 and not compaction.is_summarized(result)
