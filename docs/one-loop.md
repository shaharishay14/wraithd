# One loop, not a team

**File:** `core/loop.py`

## One-liner

> Local Agent runs one agent with one context window. Skills carry the procedures, discovery carries the
> capabilities, and the harness enforces limits and approvals. There's no orchestrator handing work to
> specialist agents.

## Why not a team of agents

The obvious design for a "personal agent that does many things" is a lead agent delegating to a calendar
agent, an email agent, a research agent. It loses on the things that matter:

- **Every hand-off loses context.** A sub-agent returns a summary; the reasoning behind it (what was tried,
  what was ruled out) stays behind. If the next step needs to know *why*, not just *what*, it belongs in the
  same agent.
- **The main reason to split is gone.** Teams split agents because one prompt got too big. Progressive
  disclosure fixes that inside one agent: skills and tools load only when needed.
- **Agents sprawl.** Overlapping specialists, a prompt stitched from conflicting rules, and a structure that
  mirrors the org chart rather than the task.

## When a sub-agent would be worth it

Only when the split follows a context boundary, the sub-agent needs little from the main conversation, and it
returns a compact result:

- independent research paths that can run in parallel
- digesting a very large document that would flood the main context
- black-box verification ("check this draft against the source")

Even then it should be a tool the main agent calls, never something that talks to the owner. And it should
stay only if the evals score better with it than without. None of the current tasks clears that bar.

## On a laptop

Nothing changed in the decision; one thing made it stronger. A local model has a smaller context window and
less patience for long prompts, so one agent with progressive disclosure fits better than several agents each
carrying their own instructions. The one model choice that does vary is per skill: a skill can ask for the
stronger configured model (`model: strong`), because agent-studio's evals found gpt-4o-mini followed subtle
skill rules less reliably than gpt-4o. That's still one loop: the run goes on with the stronger model and keeps
everything it did so far.

Sources: Anthropic, [Building agents: when to fold sub-agents into your main agent loop](https://claude.com/resources/articles/building-agents-when-to-fold-subagents-into-your-main-agent-loop); [The hard problem in multi-agent is context transfer](https://michaellivs.com/blog/multi-agent-context-transfer/).
