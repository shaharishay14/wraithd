# The harness

The Agents SDK runs the loop: call the model, run the tools it asks for, feed the results back. Everything
around that loop is the harness: what the model is given, what it may do, when it stops, what it remembers,
and how we know what happened. These notes explain each part and why it's built that way.

The harness is ported from agent-studio (a multi-user web app on EC2), where these notes started. Each note
says what changed for running on one person's laptop.

## One-liner

> Local Agent's harness logs every step of a run as an event, derives the run's state from that log, and uses
> it to stop runaway runs, steer the model with short reminders, and enforce safety rules in code. The model
> gets a small prompt and pulls in tools, memory and skills only when it needs them.

## The picture

```
            ┌──────────────────────── Harness (src/local_agent/core/) ──────────────────────┐
 input ───▶ │  Event log ──▶ Views ──▶ Stop rules / Reminders ──▶ what the model sees next  │ ──▶ model
 (terminal, │      ▲                                                          │             │
 Telegram,  │      └──────────────── tool calls and results ◀─────────────────┘             │
 schedule)  │                                                                                │
            │  Permission gate (hard rules)   Disclosure (app actions, memory, skills, web)  │
            │  Secret redaction · Egress checks · Budgets                                    │
            └────────────────────────────────────────────────────────────────────────────────┘
                       │
                       ▼
               Store (src/local_agent/store/): events, conversations, schedules, approvals in one SQLite file
```

## The notes

| Note | In one line |
|---|---|
| [One run](run.md) | `run()`, config and keys, providers, the terminal chat. |
| [Event log](event-log.md) | Every step is an event; the run's state is a query over them. |
| [Security](security.md) | Hard rules in code: the gate, taint tracking, egress, redaction, budgets. |
| [From EC2 to laptop](ec2-to-laptop.md) | What changed in the threat model, the mechanism and test for each, and `audit`. |
| [Compaction](compaction.md) | The newest 10 messages word for word, older ones folded into a stored summary. |
| [System reminders](reminders.md) | Short hints at the moment they're relevant; nudges, not rules. |
| [Progressive disclosure](progressive-disclosure.md) | Search → View → Use, so the prompt doesn't grow with every tool. |
| [Memory](memory.md) | Markdown files, a keyword map instead of vectors, BM25, distill, trust labels. |
| [Skills](skills.md) | One line in the prompt, instructions on demand, the owner's rules on top, `model: strong`. |
| [Workspace files](files.md) | List, read, write in one folder; real-path checks keep everything else out. |
| [The shell](shell.md) | Off by default; every command asks y/N; never unattended. |
| [The web](web.md) | Pages through the egress guard, search through the model provider's own (no extra key), all third-party content. |
| [Schedules](schedules.md) | Cron or one time in the owner's timezone, approved, fired at most once per slot. |
| [MCP](mcp.md) | Your apps as MCP servers: secrets per server, discovery, read-only runs, the rest asks. |
| [Installing and running](install.md) | The release build, `onboard`, the background service, `doctor`, `update`. |
| [The daemon](daemon.md) | One process: the API and UI, the scheduler, Telegram, MCP, background distill. |
| [The local API](api.md) | The contract the web UI is built on: token, Origin and Host checks, endpoints. |
| [Telegram](telegram.md) | A paired owner chat only; strangers ignored; approvals as buttons; photos, voice notes, files. |
| [The scheduler](scheduler.md) | Due schedules run unattended every minute; the answer goes to the owner. |
| [One loop, not a team](one-loop.md) | Why there's one agent, and when a sub-agent would be worth it. |
| [Proof](proof.md) | Unit tests, scripted runs through the real SDK, and replay of recorded runs. |
| [Evals](evals.md) | Ten golden tasks in a fake world of MCP servers, graded by checks and a judge, cost-capped. |

## Where the ideas come from

Most of the design follows Michael Livshits' writing on agent harnesses ([michaellivs.com/blog](https://michaellivs.com/blog)):
the harness as the layer around the loop, the conversation as an event stream, Search → View → Use, skills as
progressive disclosure, napkin's memory design, and system reminders. The "one loop" decision follows
Anthropic's guidance on folding sub-agents into the main loop. Each note links the posts it uses.
