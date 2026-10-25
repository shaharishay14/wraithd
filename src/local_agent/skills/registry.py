"""
The skill registry: built-in skills from library/, plus the owner's own from workspace/skills/, filtered to what
this run can actually do (a skill that needs email isn't offered when nothing can read email).

On disk, in workspace/skills/:

    my-skill/SKILL.md          the owner's skill (replaces a built-in one with the same name)
    my-skill/*.md              files the skill can point to (skill_load with "file")
    research-report/your-way.md  the owner's rules for a skill, one "- rule" per line, shown after its steps
    _proposed/<name>/SKILL.md  drafts the agent wrote: off until the owner moves the folder up into skills/
"""

import logging
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

LIBRARY = Path(__file__).parent / 'library'
PROPOSED = '_proposed'
YOUR_WAY = 'your-way.md'
NAME = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')
FRONTMATTER = re.compile(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', re.DOTALL)
MAX_DESCRIPTION = 300
MODELS = ('strong',)  # what a skill may ask for in `model:`; anything else is ignored


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    instructions: str
    requires: tuple[str, ...] = ()
    files: dict[str, str] = field(default_factory=dict)  # extra files (examples, references): name -> text
    source: str = 'builtin'  # builtin | user
    adjustments: tuple[str, ...] = ()  # the owner's own rules for this skill ("Your way")
    model: str | None = None  # 'strong': the run switches to the stronger configured model when it loads

    def full_instructions(self) -> str:
        """The instructions as skill_load returns them: the skill, then the owner's way of doing it."""
        if not self.adjustments:
            return self.instructions
        rules = '\n'.join(f'- {rule}' for rule in self.adjustments)
        return (f'{self.instructions}\n\n## Your way\nThe user\'s own rules for this skill, from their feedback. '
                f'Where they conflict with the steps above, these win:\n{rules}')


def parse_skill_md(text: str) -> tuple[dict, str]:
    """Splits a SKILL.md into its YAML frontmatter and the instructions below it."""
    match = FRONTMATTER.match(text or '')
    if not match:
        raise ValueError('A SKILL.md starts with a --- frontmatter block (name, description).')
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError('The frontmatter must be key: value pairs.')
    return meta, match.group(2).strip()


def validate(name: str, description: str, instructions: str) -> str | None:
    """The problem with a skill definition, or None if it's fine."""
    if not NAME.match(name or ''):
        return f'Skill name {name!r} must be kebab-case, e.g. "weekly-report".'
    if not (description or '').strip():
        return 'A skill needs a one-line description of what it does and when to use it.'
    if len(description) > MAX_DESCRIPTION:
        return f'The description is {len(description)} characters; keep it under {MAX_DESCRIPTION}.'
    if not (instructions or '').strip():
        return 'A skill needs instructions.'
    return None


def read_skill(folder: Path, source: str) -> SkillSpec:
    """A skill folder as a SkillSpec. Raises ValueError with what's wrong."""
    meta, body = parse_skill_md((folder / 'SKILL.md').read_text())
    name = meta.get('name') or folder.name
    problem = validate(name, str(meta.get('description') or ''), body)
    if problem:
        raise ValueError(problem)
    files = {extra.name: extra.read_text() for extra in sorted(folder.glob('*.md'))
             if extra.name not in ('SKILL.md', YOUR_WAY)}
    model = meta.get('model') if meta.get('model') in MODELS else None
    return SkillSpec(name=name, description=str(meta['description']).strip(), instructions=body,
                     requires=tuple(str(r) for r in meta.get('requires') or ()), files=files, source=source,
                     model=model)


@lru_cache(maxsize=1)
def builtin_skills() -> tuple[SkillSpec, ...]:
    skills = []
    for path in sorted(LIBRARY.glob('*/SKILL.md')):
        try:
            skills.append(read_skill(path.parent, 'builtin'))
        except ValueError as e:
            raise ValueError(f'{path}: {e}') from None  # a broken built-in is our bug: fail loudly
    return tuple(skills)


def requirements_met(requires, available: set[str]) -> bool:
    """Each requirement is one name or alternatives joined with '|' ("GMAIL|OUTLOOK": either will do)."""
    return all(any(option.strip() in available for option in requirement.split('|')) for requirement in requires)


def capabilities(tool_names, owner: bool) -> set[str]:
    """
    What this run can use: its tools by name, plus memory and 'owner' when the owner is the one talking (for
    skills that change the owner's setup, like skill-writer). Phase 4 adds connected apps.
    """
    found = set(tool_names)
    if owner:
        found |= {'memory', 'owner'}
    return found


class SkillFolder:
    """The owner's skills, proposals and adjustments in workspace/skills/."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def user_skills(self) -> list[SkillSpec]:
        skills = []
        for path in sorted(self.root.glob('*/SKILL.md')):
            if path.parent.name.startswith(('_', '.')):
                continue
            try:
                skills.append(read_skill(path.parent, 'user'))
            except ValueError as e:
                # The owner's file: say what's wrong and carry on without it, rather than refuse to run
                logger.warning(f'Skipping {path}: {e}')
        return skills

    def adjustments(self) -> dict[str, list[str]]:
        """The owner's rules, by skill name, in file order."""
        rules = {}
        for path in sorted(self.root.glob(f'*/{YOUR_WAY}')):
            lines = [line[2:].strip() for line in path.read_text().splitlines() if line.startswith('- ')]
            if lines:
                rules[path.parent.name] = lines
        return rules

    def add_adjustment(self, skill: str, rule: str) -> None:
        if not NAME.match(skill):
            raise ValueError(f'Not a skill name: {skill!r}')
        path = self.root / skill / YOUR_WAY
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text() if path.exists() else (
            f'# Your way: {skill}\n\nYour rules for this skill, shown to the agent after its steps. '
            'Edit or delete lines freely.\n\n')
        path.write_text(existing.rstrip('\n') + f'\n- {rule}\n')

    def is_active(self, name: str) -> bool:
        return (self.root / name / 'SKILL.md').exists()

    def propose(self, name: str, description: str, instructions: str, requires, detail: str) -> Path:
        """Saves a draft in _proposed/. The owner turns it on by moving the folder into skills/."""
        folder = self.root / PROPOSED / name
        folder.mkdir(parents=True, exist_ok=True)
        meta = {'name': name, 'description': description, 'requires': list(requires),
                'proposed_by': detail}
        (folder / 'SKILL.md').write_text(f'---\n{yaml.safe_dump(meta, sort_keys=False).strip()}\n---\n'
                                         f'{instructions.strip()}\n')
        return folder


def available_skills(user_skills, available: set[str], adjustments: dict[str, list[str]] | None = None) \
        -> list[SkillSpec]:
    """
    Built-in and the owner's skills this run can use, each with the owner's adjustments.
    An owner's skill replaces a built-in one of the same name.
    """
    by_name = {skill.name: skill for skill in builtin_skills()}
    for skill in user_skills:
        by_name[skill.name] = skill
    adjustments = adjustments or {}
    return [replace(skill, adjustments=tuple(adjustments.get(name, ())))
            for name, skill in sorted(by_name.items()) if requirements_met(skill.requires, available)]


def registry_prompt(skills: list[SkillSpec], can_adjust: bool = False) -> str:
    """One line per skill. The instructions (and the owner's adjustments) stay out of the prompt until skill_load."""
    if not skills:
        return ''
    lines = ['\nSKILLS (instructions for tasks you do often). When a request matches one, call skill_load with its '
             'name first and follow what it says:']
    lines += [f'- {skill.name}: {skill.description}' + (' (personalized)' if skill.adjustments else '')
              for skill in skills]
    if can_adjust:
        lines.append('When the user corrects how you did a skill\'s task in a way that should hold every time '
                     '("always...", "never...", "put X first"), call skill_adjust with the rule: it asks them to save '
                     'it. A preference for everything, not one task, goes to memory instead.')
    return '\n'.join(lines) + '\n'
