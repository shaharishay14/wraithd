"""
Skills: the registry (built-ins parse, requirements filter what a run is offered, the owner's skills on disk),
progressive disclosure in real runs (descriptions in the prompt, instructions on skill_load, files on request),
and proposals that stay off until the owner turns them on.
"""

import json

from local_agent.core import events as ev
from local_agent.skills import registry
from tests.scripted import ScriptedModel, call, fake_tool, of_type, run_once, say, tool_json

WEB = fake_tool('web_search', lambda **a: {'results': []})


def write_skill(home, name, description='d', body='Do it.', **meta):
    folder = home.skills_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    extra = ''.join(f'{key}: {value}\n' for key, value in meta.items())
    (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: {description}\n{extra}---\n{body}\n')
    return folder


# --- registry ----------------------------------------------------------------------------------------------

def test_every_builtin_skill_parses_and_is_short():
    skills = {skill.name: skill for skill in registry.builtin_skills()}
    assert set(skills) == {'morning-brief', 'inbox-triage', 'meeting-prep', 'research-report', 'weekly-review',
                           'skill-writer'}
    for skill in skills.values():
        assert registry.validate(skill.name, skill.description, skill.instructions) is None
        assert len(skill.instructions) < 2500, f'{skill.name} should stay compact'
    assert list(skills['research-report'].files) == ['report-template.md']
    # the eval finding: inbox triage's reply-slot rule needs the stronger model
    assert skills['inbox-triage'].model == 'strong' and skills['morning-brief'].model is None


def test_requirements_accept_alternatives():
    assert registry.requirements_met(['GMAIL|OUTLOOK'], {'OUTLOOK'})
    assert not registry.requirements_met(['GMAIL|OUTLOOK', 'memory'], {'GMAIL'})
    assert registry.requirements_met([], set())


def test_a_run_is_only_offered_skills_it_can_do():
    def names(available):
        return [s.name for s in registry.available_skills([], available)]
    assert names(set()) == []
    assert names({'memory', 'web_search'}) == ['research-report', 'weekly-review']
    assert names(registry.capabilities([], owner=True)) == ['skill-writer', 'weekly-review']
    assert names({'email'}) == ['inbox-triage', 'morning-brief']  # what a connected server provides
    assert names(registry.capabilities([], owner=False, provides=['calendar'])) == ['meeting-prep']


def test_an_owner_skill_replaces_the_builtin_with_its_name(home):
    write_skill(home, 'morning-brief', 'My own brief', 'Just the calendar.')
    folder = registry.SkillFolder(home.skills_dir)
    brief = next(s for s in registry.available_skills(folder.user_skills(), set()) if s.name == 'morning-brief')
    assert brief.instructions == 'Just the calendar.' and brief.source == 'user'


def test_a_broken_owner_skill_is_skipped_not_fatal(home, caplog):
    (home.skills_dir / 'broken').mkdir()
    (home.skills_dir / 'broken' / 'SKILL.md').write_text('no frontmatter here')
    write_skill(home, 'Bad Name')
    write_skill(home, 'fine-one', model='weak')  # an unknown model is ignored, not an error
    skills = registry.SkillFolder(home.skills_dir).user_skills()
    assert [s.name for s in skills] == ['fine-one'] and skills[0].model is None
    assert 'Skipping' in caplog.text


def test_the_prompt_lists_descriptions_never_instructions():
    skills = registry.available_skills([], {'memory', 'web_search'})
    prompt = registry.registry_prompt(skills)
    assert '- research-report: Research a question' in prompt
    assert all(skill.instructions[:60] not in prompt for skill in skills)


# --- in runs -----------------------------------------------------------------------------------------------

async def test_a_matching_request_loads_the_skill_then_its_file(make_runtime):
    model = ScriptedModel([
        [call('1', 'skill_load', {'name': 'research-report'})],
        [call('2', 'skill_load', {'name': 'research-report', 'file': 'report-template.md'})],
        [say('# Report')],
    ])
    _, events = await run_once(make_runtime(model), 'research solid-state batteries', tools=[WEB])

    assert '- research-report:' in model.instructions[0] and 'Prefer primary sources' not in model.instructions[0]
    loaded = tool_json(model.tool_outputs(1)['call_1'])
    assert 'Prefer primary sources' in loaded['instructions'] and loaded['files'] == ['report-template.md']
    assert json.loads(model.tool_outputs(2)['call_2'])['content'].startswith('# <Question')
    assert 'research-report' in of_type(events, ev.CONTEXT_BUILT)[0].payload['skills_offered']


async def test_unknown_skills_get_suggestions(make_runtime):
    model = ScriptedModel([[call('1', 'skill_load', {'name': 'skill-writr'})], [say('ok')]])
    await run_once(make_runtime(model), 'make this a skill')
    assert json.loads(model.tool_outputs(1)['call_1'])['did_you_mean'] == ['skill-writer']


async def test_a_proposed_skill_is_off_until_the_owner_turns_it_on(make_runtime, home):
    model = ScriptedModel([
        [call('1', 'skill_propose', {'name': 'weekly-sales-digest', 'description': 'Sum up the week in sales.',
                                     'instructions': '1. Read the numbers. 2. Summarize.', 'requires': []})],
        [say('Drafted it for your review.')],
    ])
    await run_once(make_runtime(model), 'make this a skill')

    proposal = home.skills_dir / '_proposed' / 'weekly-sales-digest'
    assert 'proposed_by: run ' in (proposal / 'SKILL.md').read_text()
    assert 'moving the folder' in json.loads(model.tool_outputs(1)['call_1'])['hint']

    model = ScriptedModel([[say('hi')]])
    await run_once(make_runtime(model), 'hi')
    assert 'weekly-sales-digest' not in model.instructions[0]  # not offered while proposed

    proposal.rename(home.skills_dir / 'weekly-sales-digest')
    model = ScriptedModel([[say('hi')]])
    await run_once(make_runtime(model), 'hi')
    assert '- weekly-sales-digest: Sum up the week in sales.' in model.instructions[0]


async def test_the_agent_cannot_overwrite_an_active_skill(make_runtime, home):
    write_skill(home, 'my-skill', body='mine')
    model = ScriptedModel([[call('1', 'skill_propose', {'name': 'my-skill', 'description': 'd',
                                                        'instructions': 'evil'})], [say('ok')]])
    await run_once(make_runtime(model), 'x')
    assert 'already has an active skill' in json.loads(model.tool_outputs(1)['call_1'])['error']
    assert 'mine' in (home.skills_dir / 'my-skill' / 'SKILL.md').read_text()
    assert not (home.skills_dir / '_proposed' / 'my-skill').exists()


async def test_strangers_get_builtins_but_not_the_owners_skills(make_runtime, home):
    write_skill(home, 'family-plans', 'Plans with my kids', 'private')
    model = ScriptedModel([[say('hi')]])
    await run_once(make_runtime(model), 'hi', owner=False)
    assert 'family-plans' not in model.instructions[0]
    # no skill fits a plain run for a stranger, so no skill tools at all
    assert not any(name.startswith('skill_') for name in model.tool_names[0])
