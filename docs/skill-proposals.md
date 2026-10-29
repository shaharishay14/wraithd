# Skill proposals

**Files:** `skills/review.py`, `skills/tools.py` (`skill_propose`), the proposal routes in `daemon/api.py`

## One-liner

> A skill is a standing instruction, so a draft the agent writes waits on the review queue like a memory from an
> email does. A draft from a run that read someone else's text is flagged.

## How it works

- **Proposing:** `skill_propose` saves the draft in `workspace/skills/_proposed/<name>/` (inactive, as before) and
  queues a `skill` item: `{name, description, instructions, requires, detail, flagged, fingerprint}`, with the run
  and the third-party sources it had read. A flagged item's summary starts `⚠ from a run that read ...`, and
  `review show` says to read the instructions first. A newer draft of the same name replaces the pending one.
- **Approving**, any of three ways, settles the others:
  - `local-agent review approve ID` (or `POST /api/review/{id}/approve`) moves the folder into `workspace/skills/`;
  - the web UI's Skills page (`POST /api/skills/proposals/{name}/approve`) moves it, then settles the item;
  - moving the folder by hand: the daemon's `skill_settle` job (every minute) marks the item approved.
- **Rejecting** (or deleting on the Skills page) deletes the draft; nothing is written.
- **The rules hold:** the name must be a skill name, the draft folder and every file in it must stay inside it (no
  symlinks out), an active skill with the name is never overwritten, and a draft that changed after it was queued
  isn't turned on (its fingerprint).
- Once approved, it's an owner's skill: listed in the prompt and loaded with `skill_load` on a matching request.
