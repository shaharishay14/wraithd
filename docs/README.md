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
               Store (src/local_agent/store/): events and approvals in one SQLite file
```

## The notes

| Note | In one line |
|---|---|
| [Event log](event-log.md) | Every step is an event; the run's state is a query over them. |
| [Security](security.md) | Hard rules in code: the gate, taint tracking, egress, redaction, budgets. |
| [System reminders](reminders.md) | Short hints at the moment they're relevant; nudges, not rules. |
| [Progressive disclosure](progressive-disclosure.md) | Search → View → Use, so the prompt doesn't grow with every tool. |
| [One loop, not a team](one-loop.md) | Why there's one agent, and when a sub-agent would be worth it. |
| [Proof](proof.md) | Unit tests, scripted runs through the real SDK, and replay of recorded runs. |

Memory, skills and background runs get their notes when they're ported (Phases 2 to 5).

## Where the ideas come from

Most of the design follows Michael Livshits' writing on agent harnesses ([michaellivs.com/blog](https://michaellivs.com/blog)):
the harness as the layer around the loop, the conversation as an event stream, Search → View → Use, skills as
progressive disclosure, napkin's memory design, and system reminders. The "one loop" decision follows
Anthropic's guidance on folding sub-agents into the main loop. Each note links the posts it uses.
