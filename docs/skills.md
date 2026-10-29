# Skills

**Files:** `skills/registry.py`, `skills/tools.py`, `skills/library/*/SKILL.md`

## One-liner

> A skill is a how-to for one kind of task. The prompt lists one line per skill; `skill_load` brings in the
> instructions when a request needs them. The owner's way of doing it is layered on top as their own rules,
> saved from their corrections with one "y".

## How it works

- **Format:** `SKILL.md` with frontmatter (`name`, `description`, `requires`, optionally `model`) and
  instructions. Built-in skills live in the package (`skills/library/`); the owner's are folders in
  `workspace/skills/` and replace a built-in one of the same name.
- **Filtering:** `requires: [email]` names a capability: a connected MCP server that provides it (`mcp add
  --provides email`), a tool (`web_search`), `memory` or `owner`; `a|b` means either will do. A run is only
  offered skills it can actually do, so without email nobody sees inbox-triage.
- **Disclosure:** the prompt holds names and descriptions; `skill_load(name)` returns the instructions;
  `skill_load(name, file=...)` returns a file the skill points to (a third level).
- **Starter set:** morning-brief, inbox-triage, meeting-prep, research-report, weekly-review, skill-writer. Short
  on purpose: steps, rules and gotchas only, nothing a capable model knows already.

## The strong model

agent-studio's evals found that gpt-4o-mini followed subtle skill rules less reliably than gpt-4o: inbox triage
kept answering for the user in draft replies instead of leaving a slot. So a skill can say `model: strong`
(inbox-triage does). When a run loads such a skill, the hook stops before the next model call and the loop goes
on with the stronger configured model, with everything the run did so far. It's logged as a `model_upgrade`
event. Chit-chat stays on the cheap model; without a separate strong model configured, nothing switches.

## Personalization: "your way"

Everyone does the same task differently, and most people won't write a skill. So personalization comes from
corrections:

1. The owner corrects a skill's output ("skip newsletters, Slack first").
2. A reminder nudges the agent; it calls `skill_adjust(skill, rule)`.
3. That's a write, so the permission gate asks: `Allow? [y/N]`. One "y" saves it.
4. From then on `skill_load` returns the instructions plus a "Your way" section that wins conflicts.

Rules on top rather than a private copy: the built-in skill keeps improving underneath, the rules are short
enough to read and undo, and they explain why the agent does what it does.

## Skills the agent writes

`skill_propose` saves a draft in `workspace/skills/_proposed/<name>/` and queues it for the owner's review: off
until they approve it (`local-agent review approve ID`, the Skills page, or moving the folder into
`workspace/skills/`), and it can't overwrite an active skill. A draft from a run that read third-party content is
flagged ([skill proposals](skill-proposals.md)). Adjustments need the owner's "y"; unattended
runs can't change a skill at all. So a run that read a malicious email can't give the agent new standing
instructions.

## On a laptop

- **Rows became folders.** The owner's skills, rules and proposals are plain files: `my-skill/SKILL.md`,
  `research-report/your-way.md` (one `- rule` per line, edit or delete freely), `_proposed/<name>/SKILL.md`.
  Turning a proposal on is moving a folder; there's no Skills page to click.
- **A broken skill of the owner's is skipped with a warning**, not fatal; a broken built-in is our bug and fails
  loudly.
- **A skill stays in its folder.** A skill cloned from someone's repo can carry symlinks (git keeps them):
  `reference.md -> ~/.ssh/id_rsa`. Any file (or `your-way.md`) whose real path is outside the skill's folder is
  skipped with a warning, and a `SKILL.md` that points out skips the skill. `skill_load`'s `file` must be one of
  the files listed, so `../` and absolute paths find nothing.
- **Removing a rule deletes it.** agent-studio kept removed rules for the history; here the file is the history,
  and putting it in git keeps one.
- **Requirements name capabilities, not apps.** agent-studio wrote `GMAIL|OUTLOOK`; here a skill says `email`,
  and whichever server the owner connected for mail provides it ([MCP](mcp.md)). The morning brief needs only
  email now; it uses a calendar when one is connected.

## Skills vs memory

Memory is *what* the agent knows about you. Skills are *how* it does things. A preference for everything goes to
the profile; a preference for one task is a skill rule.

Source: [Skills aren't the innovation](https://michaellivs.com/blog/skills-arent-the-innovation/), [Knowledge distillation into skills](https://michaellivs.com/blog/knowledge-distillation-into-skills/), [Skills, forks, and self-surgery](https://michaellivs.com/blog/three-ways-to-extend-an-agent-harness/).
