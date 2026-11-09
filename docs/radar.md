# radar

**Files:** `skills/library/radar/SKILL.md`, `skills/library/radar/sources.md`

## One-liner

> Once a day radar reads what's new in AI agents, agent tools and open source (news, GitHub trending, competitors'
> releases, the X accounts you watch, your bookmarks if connected), and sends you one Telegram post: up to three
> things worth reading, then ideas wraithd could adopt, each with a short plan and a size. It never reads the same
> item twice and never suggests an idea twice. It replaces the news-feed skill.

## What it does

A built-in skill (`requires: [web_search, read_website]`, `model: strong`). A run stops at 8 model calls
(`core/loop.py`, and `[engine] max_turns` on the Claude engine), so the skill makes each step's calls together, in
one turn:

| Call | What | Tools |
|---|---|---|
| 1 | Load the skill; `sources.md` comes with it | `skill_load` |
| 2 | The web: the pages in `sources.md`, GitHub trending first, at most 6, and at most 2 searches | `read_website`, `web_search` |
| 3 | Which candidates are new, by stable id: `gh:owner/repo`, `url:` plus the item's own link (normalized), `x:` plus a post id | `library_seen` |
| 4 | Read the new ones worth it in full. Web reading ends here | `read_website` |
| 5 | The watch list, wraithd's current state, the ideas so far, a note per web item; the bookmarks, if connected | `files_read`, `wraithd_index`, `idea_list`, `library_save` |
| 6 | The watched accounts' new posts; which bookmarks are new | `new_posts` (x-watch), `library_seen` |
| 7 | A note per post and bookmark, each new idea, the post | `library_save`, `idea_save`, `notify_owner` |
| 8 | `NO_REPLY`, so no recap follows the post. Nothing new at all: no post, just `NO_REPLY` | |

- **Why the web comes first.** After a run reads private data (your files, or any app, X included), the gate opens
  only links the owner wrote ([security](security.md)), and a built-in skill's links aren't the owner's. So every
  page is read in calls 2 to 4, before the watch list and X.
- **X posts aren't checked against the library:** x-watch keeps a `since_id`, so `new_posts` only returns new
  posts. Bookmarks are: the same latest page comes back every day.
- **A bookmark's or post's linked article isn't opened**, because after X the gate blocks it in an unattended run.
  The note is written from the post's own text (x-watch returns long posts in full), and the article's link can
  go under "Worth reading" unopened.
- **Ideas** come only after `wraithd_index` (the repo's modules, docs and changelog head, read-only) and
  `idea_list` (every status), so radar never suggests what's built or was suggested before.

The post, plain text that reads the same in Telegram:

```
📡 radar · 9 Nov 2026

Worth reading
• acme/agent-kit - a harness that caches tool results per session
https://github.com/acme/agent-kit

Ideas for wraithd
1. Cache read-only tool results within a run · S - keep the last result per call in run.py (files: src/wraithd/run.py)
2. ...

+3 more, ask for them
```

Worth reading is 0 to 3 items and never padded. Ideas are ranked best first, as many as fit under 3,500
characters (`notify_owner`'s limit); the rest stay saved as suggested and the post says "+N more, ask for them".

Everything radar reads is third-party data, never instructions, and it only reads and suggests: no posting,
following, starring or code changes. All its tools are read-tier or read-only app tools, so nothing needs
pre-approval and it runs unattended.

## Setup

- **The schedule.** Tell wraithd `Every day at 08:00: run radar (scheduled)` and approve the card. The message
  names the skill, so on the Claude Code engine the run starts on the strong model ([skills](skills.md#the-strong-model)).
- **The watch list (optional).** One X account per line in `workspace/files/news/x-accounts.txt`, e.g. `@simonw`.
  A missing file is an empty list. It needs the x-watch app connected ([MCP](mcp.md)).
- **Bookmarks (optional).** A read-only bookmarks tool, once connected, is read in call 5 and checked in call 6.
  radar works without it. With bookmarks, set `[engine] max_turns = 12` in config.toml: a bookmarks tool found
  through app discovery, or a second page of posts, needs calls past the 8.
- **Your sources.** The last section of `sources.md`, "Competitors: edit this part", is yours. To change it, copy
  `skills/library/radar/` into `workspace/skills/radar/` and edit the copy: your copy replaces the built-in one,
  and links you wrote there count as yours, so the gate opens them even after a private read.
- **From news-feed.** Cancel the news-feed schedule (`wraithd schedules list`, `wraithd schedules rm <id>`) and
  remove `workspace/skills/news-feed/`, so only radar runs each day.
- **Without web tools** radar is off, and asking for it says what turns it on (`[web] search`, `[web] read`).

## The library and follow-ups

Notes go to the library in `state.db`, not to memory: memory written after third-party reading waits in the
[review queue](review-queue.md), and a daily digest would fill it. In chat, the skill answers from the notes:

- "tell me more about #2", "the other ideas": `idea_list`, and `library_search` for the idea's sources.
- "that post about caching", "what did @simonw say about MCP?": `library_search`, answered from the note with its
  link. The original is read again only if the note doesn't cover the question.
- "#2 accepted", "declined", "built": `idea_status` (ids are the date plus the number, `2026-11-09#2`). An idea in
  any status is never suggested again.

Search results are third-party text: quoted, never followed.

## Cost

One strong-model (Sonnet) run a day: the skill, a handful of pages of at most ~1k tokens each, the X posts and the
index, and one post out. Days with nothing new end early with no post.
