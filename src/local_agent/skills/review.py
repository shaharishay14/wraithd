"""
Skill proposals that wait for the owner (the review queue's 'skill' kind).

skill_propose saves a draft in workspace/skills/_proposed/<name>/ and queues it here. A skill is a standing
instruction: once on, it's offered in every matching run. So nothing the agent drafts is on until the owner says
yes, and a draft from a run that read third-party content is flagged, since its words may be someone else's.

There are three ways to say yes, and each one settles the others:
- `local-agent review approve ID` (or the review API): moves the folder into skills/
- the web UI's Skills page (the old /api/skills/proposals routes): the route moves it, then calls settle()
- moving the folder by hand: the daemon's settle job (every minute) marks the item approved

Rejecting deletes the draft. Neither path writes outside workspace/skills/, and a draft whose folder holds a symlink
pointing out of it is refused.
"""

import hashlib
import shutil
from pathlib import Path

from local_agent.review import queue
from local_agent.skills import registry

KIND = 'skill'
SUMMARY_TEXT = 160
SETTLE_EVERY_SECONDS = 60
SETTLED_BY = 'skills-folder'  # decided_by when the owner moved or deleted the folder themselves


def fingerprint(path: Path) -> str:
    """A SKILL.md as it is now ('' when there's none), so a draft that changed after it was queued is caught."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, NotADirectoryError):
        return ''


def proposal_dir(root: Path, name: str) -> Path:
    return Path(root) / registry.PROPOSED / name


def queue_proposal(store, root: Path, *, name: str, description: str, instructions: str, requires, detail: str,
                   run_id: str | None, taint) -> queue.ReviewItem:
    """Queues a draft skill_propose just saved. An older pending draft of the same name is replaced by this one."""
    taint = list(taint)
    for older in pending_for(store, name):
        queue.reject(store, None, older.id, by='agent', reason='replaced by a newer proposal')
    one_line = ' '.join(description.split())
    one_line = one_line if len(one_line) <= SUMMARY_TEXT else one_line[:SUMMARY_TEXT - 3] + '...'
    flag = f'⚠ from a run that read {", ".join(taint)}: ' if taint else ''
    content = {'name': name, 'description': description, 'instructions': instructions, 'requires': list(requires),
               'detail': detail, 'flagged': bool(taint),
               'fingerprint': fingerprint(proposal_dir(root, name) / 'SKILL.md')}
    return queue.ReviewQueue(store).add(KIND, content, f'{flag}New skill {name}: {one_line}', 'agent', run_id=run_id,
                                        taint=taint)


def pending_for(store, name: str) -> list[queue.ReviewItem]:
    return [item for item in queue.ReviewQueue(store).list(queue.PENDING, KIND) if item.content.get('name') == name]


def activate(root: Path, name: str) -> Path:
    """
    Moves a draft from _proposed/ into skills/, as the Skills page always has. Raises ReviewError when the name
    isn't a skill name, the draft points outside its folder, or an active skill already has the name.
    """
    root = Path(root)
    if not registry.NAME.match(name or ''):
        raise queue.ReviewError(f'{name!r} is not a skill name.')
    folder = proposal_dir(root, name)
    if folder.is_symlink() or not registry.inside(root, folder):
        raise queue.ReviewError(f'The draft {name} points outside workspace/skills/, so it is not turned on.')
    outside = [item.name for item in folder.iterdir() if item.is_symlink() or not registry.inside(folder, item)]
    if outside:
        raise queue.ReviewError(f'The draft {name} has files that point outside its folder ({", ".join(outside)}), '
                                'so it is not turned on.')
    if (root / name / 'SKILL.md').exists():
        raise queue.ReviewError(f'There is already an active skill named {name}.')
    target = root / name
    if target.exists():  # a folder with only the owner's rules for a built-in: keep them with the new skill
        for item in folder.iterdir():
            shutil.move(str(item), target / item.name)
        folder.rmdir()
    else:
        folder.rename(target)
    return target


def _root(ctx: queue.ReviewContext) -> Path:
    return ctx.home.skills_dir


def approve(item: queue.ReviewItem, ctx: queue.ReviewContext) -> str:
    name, queued = item.content['name'], item.content.get('fingerprint', '')
    root = _root(ctx)
    draft = proposal_dir(root, name) / 'SKILL.md'
    if draft.exists():
        if fingerprint(draft) != queued:
            raise queue.ReviewError(f'The draft {name} changed after it was queued. Read it again in {draft.parent} '
                                    'and turn it on there, or reject this item.')
        activate(root, name)
        return f'turned on skill {name} (workspace/skills/{name})'
    if queued and fingerprint(root / name / 'SKILL.md') == queued:
        return f'skill {name} was already turned on'  # the Skills page, or the owner moving the folder
    raise queue.ReviewError(f'The draft {name} is gone (deleted or changed by hand). Reject this item.')


def reject(item: queue.ReviewItem, ctx: queue.ReviewContext | None) -> None:
    """Deletes the draft, but only the one this item queued: a newer draft of the same name stays."""
    if ctx is None or ctx.home is None:
        return
    folder = proposal_dir(_root(ctx), item.content['name'])
    if fingerprint(folder / 'SKILL.md') == item.content.get('fingerprint') and not folder.is_symlink() \
            and registry.inside(_root(ctx), folder):
        shutil.rmtree(folder)


def show(item: queue.ReviewItem) -> str:
    content = item.content
    lines = [f'Skill: {content["name"]}', f'Description: {content["description"]}']
    if content.get('requires'):
        lines.append(f'Needs: {", ".join(content["requires"])}')
    if content.get('flagged'):
        lines.append(f'⚠ Drafted in a run that read third-party content ({", ".join(item.taint)}). Its instructions '
                     'may be someone else\'s words: read them before you approve.')
    lines += ['', content['instructions']]
    return '\n'.join(lines)


def settle(store, home, name: str, approved: bool, by: str = 'api') -> list[queue.ReviewItem]:
    """The Skills page (or the owner's own hands) turned a draft on or deleted it: settle its pending items."""
    decided = []
    for item in pending_for(store, name):
        try:
            decided.append(queue.approve(store, home, item.id, by) if approved
                           else queue.reject(store, home, item.id, by, reason='deleted on the Skills page'))
        except queue.ReviewError:
            continue  # approved by someone else meanwhile, or the active skill isn't this draft
    return decided


def settle_all(store, home) -> int:
    """Pending items whose draft is gone: approved if the skill is on with the same text, else rejected."""
    settled = 0
    for item in queue.ReviewQueue(store).list(queue.PENDING, KIND):
        name = item.content.get('name', '')
        if (proposal_dir(home.skills_dir, name) / 'SKILL.md').exists():
            continue
        active = fingerprint(home.skills_dir / name / 'SKILL.md') == item.content.get('fingerprint')
        try:
            if active:
                queue.approve(store, home, item.id, SETTLED_BY)
            else:
                queue.reject(store, home, item.id, SETTLED_BY, reason='the draft was removed')
        except queue.ReviewError:
            continue
        settled += 1
    return settled


async def settle_job(rt) -> int:
    """The daemon's periodic job: the owner moved or deleted a draft folder by hand."""
    return settle_all(rt.store, rt.home)


queue.register(KIND, approve, reject, show)
