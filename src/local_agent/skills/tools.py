"""
The agent's skill tools:
- skill_load: view a skill (with the owner's adjustments), or one of its files
- skill_propose: draft a new skill from a run that went well. Stored as a proposal: only the owner can turn it
  on, so a run that read something malicious can't teach the agent new standing instructions.
- skill_adjust: save one rule to the owner's version of a skill. It's a write, so the permission gate asks the
  owner first (the y/N prompt in the terminal), and it never runs unattended.
"""

import difflib
import json
from dataclasses import replace

from agents import FunctionTool

from local_agent.core import events as ev
from local_agent.core.gate import parse_arguments
from local_agent.core.views import SKILL_LOAD as LOAD
from local_agent.skills import registry

PROPOSE, ADJUST = 'skill_propose', 'skill_adjust'
MAX_ADJUSTMENTS = 15  # per skill: past this, the owner should tidy them in your-way.md


def skill_tools(folder: registry.SkillFolder, skills: list[registry.SkillSpec], gate, log: ev.EventLog):
    """[skill_load, skill_propose, skill_adjust], and the prechecks the guard runs before asking the owner."""
    by_name = {skill.name: skill for skill in skills}

    def detail():
        return f'run {log.run_id}' + (f', after reading {", ".join(gate.untrusted_sources)}'
                                      if gate.untrusted_sources else '')

    def unknown(name):
        return json.dumps({'error': f'There is no skill {name!r}.',
                           'did_you_mean': difflib.get_close_matches(name, by_name, n=3, cutoff=0.5)})

    async def load(ctx, input_json: str) -> str:
        arguments = parse_arguments(input_json)
        name, file = (arguments.get('name') or '').strip().lower(), arguments.get('file')
        skill = by_name.get(name)
        if skill is None:
            return unknown(name)
        if file:
            if file not in skill.files:
                return json.dumps({'error': f'{name} has no file {file!r}.', 'files': sorted(skill.files)})
            return json.dumps({'skill': name, 'file': file, 'content': skill.files[file]})
        result = {'skill': name, 'instructions': skill.full_instructions()}
        if skill.files:
            result['files'] = sorted(skill.files)
            result['hint'] = f'Load a file with {LOAD} and "file" only if the instructions call for it.'
        return json.dumps(result)

    async def propose(ctx, input_json: str) -> str:
        arguments = parse_arguments(input_json)
        name = (arguments.get('name') or '').strip().lower()
        description, instructions = arguments.get('description') or '', arguments.get('instructions') or ''
        problem = registry.validate(name, description, instructions)
        if problem:
            return json.dumps({'error': problem})
        if folder.is_active(name):
            return json.dumps({'error': f'The user already has an active skill named {name}. Propose a different '
                                        'name.'})
        requires = [str(item) for item in arguments.get('requires') or []]
        saved = folder.propose(name, description.strip(), instructions, requires, detail())
        return json.dumps({'proposed': name, 'status': 'waiting for the user',
                           'hint': f'Tell the user you drafted this skill in {saved}, and that moving the folder '
                                   f'into {folder.root} turns it on.'})

    def _adjust_arguments(arguments):
        return (arguments.get('skill') or '').strip().lower(), ' '.join((arguments.get('rule') or '').split())

    def adjust_precheck(arguments):
        """Answers a skill_adjust that can't be saved before the owner is asked, so they never approve a no-op."""
        name, rule = _adjust_arguments(arguments)
        skill = by_name.get(name)
        if skill is None:
            return unknown(name)
        if not rule or len(rule) > 300:
            return json.dumps({'error': 'The rule must be one short sentence (at most 300 characters).'})
        if rule.lower() in (existing.lower() for existing in skill.adjustments):
            return json.dumps({'saved': name, 'rule': rule, 'note': 'This rule was already saved.'})
        if len(skill.adjustments) >= MAX_ADJUSTMENTS:
            return json.dumps({'error': f'{name} already has {MAX_ADJUSTMENTS} rules. Ask the user to tidy them in '
                                        f'{folder.root / name / registry.YOUR_WAY} first.'})
        return None

    async def adjust(ctx, input_json: str) -> str:
        # Runs only after the owner approved it (the gate asks first, after adjust_precheck)
        arguments = parse_arguments(input_json)
        problem = adjust_precheck(arguments)  # again: the skill list may have changed while the prompt waited
        if problem:
            return problem
        name, rule = _adjust_arguments(arguments)
        folder.add_adjustment(name, rule)
        by_name[name] = replace(by_name[name], adjustments=by_name[name].adjustments + (rule,))  # later loads see it
        return json.dumps({'saved': name, 'rule': rule,
                           'hint': f'Tell the user it is saved, and that they can change it in '
                                   f'{folder.root / name / registry.YOUR_WAY}.'})

    tools = [
        FunctionTool(
            name=LOAD,
            description='Load the full instructions of a skill listed under SKILLS (or one of its files).',
            params_json_schema={'type': 'object', 'properties': {
                'name': {'type': 'string', 'description': 'Skill name, e.g. "morning-brief"'},
                'file': {'type': 'string', 'description': 'Optional: one of the files the skill lists'}},
                'required': ['name']},
            on_invoke_tool=load, strict_json_schema=False),
        FunctionTool(
            name=PROPOSE,
            description=('Draft a reusable skill from a task you just completed well, when the user asks for it or '
                         'agrees. The user reviews it before it can be used.'),
            params_json_schema={'type': 'object', 'properties': {
                'name': {'type': 'string', 'description': 'kebab-case, e.g. "weekly-sales-digest"'},
                'description': {'type': 'string', 'description': 'One line: what it does and when to use it'},
                'instructions': {'type': 'string', 'description': 'The steps and rules, in markdown'},
                'requires': {'type': 'array', 'items': {'type': 'string'},
                             'description': 'Tools it needs, e.g. ["memory", "web_search"]'}},
                'required': ['name', 'description', 'instructions']},
            on_invoke_tool=propose, strict_json_schema=False),
        FunctionTool(
            name=ADJUST,
            description=('Save one rule to the user\'s own version of a skill, from their feedback, so it holds every '
                         'time. The user is asked to approve it.'),
            params_json_schema={'type': 'object', 'properties': {
                'skill': {'type': 'string', 'description': 'The skill it applies to, e.g. "morning-brief"'},
                'rule': {'type': 'string', 'description': 'One short sentence in the user\'s terms, '
                                                          'e.g. "Put Slack mentions before email"'}},
                'required': ['skill', 'rule']},
            on_invoke_tool=adjust, strict_json_schema=False),
    ]
    return tools, {ADJUST: adjust_precheck}
