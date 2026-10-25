"""
Skill adjustments: the owner's own rules for a skill, saved from feedback after a y on the approval prompt, kept
in workspace/skills/<skill>/your-way.md, and shown as "Your way" whenever the skill loads.
"""

import asyncio
import json

from local_agent.skills import registry
from local_agent.store import base as store_base
from tests.scripted import ScriptedModel, call, fake_tool, run_once, say, tool_json

RULE = 'Always put the sources table first'
WEB = fake_tool('web_search', lambda **a: {'snippet': 'Tell your agent to...'})


async def decide_when_asked(rt, callback, status):
    """Plays the owner answering the approval prompt."""
    for _ in range(500):
        if any(event == 'approval_required' for event, _ in callback.recorded):
            break
        await asyncio.sleep(0.01)
    else:
        return
    approval_id = [data for event, data in callback.recorded if event == 'approval_required'][-1]['approval_id']
    rt.store.decide_approval(approval_id, status)


def adjust_turns():
    return [[call('1', 'skill_adjust', {'skill': 'research-report', 'rule': RULE})],
            [call('2', 'skill_load', {'name': 'research-report'})],
            [say('Saved.')]]


# --- the parts ---------------------------------------------------------------------------------------------

def test_adjustments_come_after_the_instructions_and_win_conflicts():
    skill = registry.SkillSpec(name='x', description='d', instructions='1. Do it.', adjustments=('Slack first',))
    text = skill.full_instructions()
    assert text.startswith('1. Do it.') and '## Your way' in text and text.endswith('- Slack first')
    assert 'these win' in text
    assert registry.SkillSpec(name='x', description='d', instructions='1. Do it.').full_instructions() == '1. Do it.'


def test_the_prompt_marks_personalized_skills_and_explains_adjusting():
    skills = registry.available_skills([], {'web_search'}, {'research-report': [RULE]})
    prompt = registry.registry_prompt(skills, can_adjust=True)
    assert '- research-report: ' in prompt and '(personalized)' in prompt and RULE not in prompt
    assert 'skill_adjust' in prompt and 'goes to memory instead' in prompt
    assert 'skill_adjust' not in registry.registry_prompt(skills)


def test_the_owner_edits_rules_in_a_plain_file(home):
    folder = registry.SkillFolder(home.skills_dir)
    folder.add_adjustment('morning-brief', 'Slack first')
    folder.add_adjustment('morning-brief', 'No weather')
    file = home.skills_dir / 'morning-brief' / registry.YOUR_WAY
    assert file.read_text().startswith('# Your way: morning-brief')
    file.write_text(file.read_text().replace('- No weather\n', ''))  # the owner deletes a line
    assert folder.adjustments() == {'morning-brief': ['Slack first']}
    # a folder with only your-way.md is not a skill of its own: the built-in stays, with the rule on top
    brief = next(s for s in registry.available_skills(folder.user_skills(), {'GMAIL', 'GOOGLECALENDAR'},
                                                      folder.adjustments()) if s.name == 'morning-brief')
    assert brief.source == 'builtin' and brief.adjustments == ('Slack first',)


# --- in chat -----------------------------------------------------------------------------------------------

async def test_one_yes_saves_the_rule_and_the_skill_follows_it(make_runtime, home, events, fast_approvals):
    model = ScriptedModel(adjust_turns())
    rt = make_runtime(model)
    await asyncio.gather(run_once(rt, 'always put the sources first', tools=[WEB], callback=events),
                         decide_when_asked(rt, events, store_base.APPROVED))

    card = next(data for event, data in events.recorded if event == 'approval_required')
    assert card['tool'] == 'skill_adjust' and card['arguments'] == {'skill': 'research-report', 'rule': RULE}
    assert 'every time it uses this skill' in card['reason']
    assert registry.SkillFolder(home.skills_dir).adjustments() == {'research-report': [RULE]}
    # in the same run, the next skill_load already has it
    assert RULE in tool_json(model.tool_outputs(2)['call_2'])['instructions']

    # and in later runs
    model = ScriptedModel([[call('3', 'skill_load', {'name': 'research-report'})], [say('ok')]])
    await run_once(make_runtime(model), 'research batteries', tools=[WEB])
    assert tool_json(model.tool_outputs(1)['call_3'])['instructions'].endswith(f'- {RULE}')


async def test_declining_saves_nothing(make_runtime, home, events, fast_approvals):
    model = ScriptedModel(adjust_turns())
    rt = make_runtime(model)
    await asyncio.gather(run_once(rt, 'sources first', tools=[WEB], callback=events),
                         decide_when_asked(rt, events, store_base.DENIED))
    assert registry.SkillFolder(home.skills_dir).adjustments() == {}
    # "just this time": not saved, but the model is told to still do it the owner's way now
    declined = json.loads(model.tool_outputs(1)['call_1'])['error']
    assert 'Still do it their way in this conversation' in declined


async def test_unattended_runs_cannot_change_a_skill(make_runtime, home):
    model = ScriptedModel(adjust_turns())
    await run_once(make_runtime(model), 'sources first', tools=[WEB], source='schedule')  # no callback: nobody to ask
    assert registry.SkillFolder(home.skills_dir).adjustments() == {}
    assert 'needs the user\'s approval' in json.loads(model.tool_outputs(1)['call_1'])['error']


async def test_after_reading_a_web_page_the_card_warns(make_runtime, events, fast_approvals):
    model = ScriptedModel([[call('0', 'web_search', {'query': 'x'})]] + adjust_turns())
    rt = make_runtime(model)
    await asyncio.gather(run_once(rt, 'look this up', tools=[WEB], callback=events),
                         decide_when_asked(rt, events, store_base.DENIED))
    card = next(data for event, data in events.recorded if event == 'approval_required')
    assert card['untrusted_sources'] == ['web_search']


async def test_a_repeated_rule_is_not_saved_twice(make_runtime, home, events):
    registry.SkillFolder(home.skills_dir).add_adjustment('research-report', RULE)
    model = ScriptedModel(adjust_turns())
    await run_once(make_runtime(model), 'sources first', tools=[WEB], callback=events)
    assert 'already saved' in json.loads(model.tool_outputs(1)['call_1'])['note']
    assert registry.SkillFolder(home.skills_dir).adjustments() == {'research-report': [RULE]}
    # answered before the gate: the owner isn't asked to approve a no-op
    assert not any(event == 'approval_required' for event, _ in events.recorded)


async def test_an_unknown_skill_is_answered_without_a_prompt(make_runtime, events):
    model = ScriptedModel([[call('1', 'skill_adjust', {'skill': 'reserch-report', 'rule': RULE})], [say('ok')]])
    await run_once(make_runtime(model), 'sources first', tools=[WEB], callback=events)
    assert json.loads(model.tool_outputs(1)['call_1'])['did_you_mean'] == ['research-report']
    assert not any(event == 'approval_required' for event, _ in events.recorded)
