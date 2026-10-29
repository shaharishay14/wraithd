# Prompt caching

**Files:** `run.py`, `providers.py`, `core/tokens.py`

## One-liner

> The tool list and the system prompt are a stable prefix: the same bytes on every turn and every run of a
> conversation, so the provider can serve them from its cache. Anything that changes goes after them.

## The prefix order (the rule for every module)

1. **Tool list.** It's in the order `run()` builds it. Anthropic puts tools before the system prompt, and OpenAI
   caches them with the prefix.
2. **System prompt**, most stable first:
   1. base instructions
   2. reminders preamble
   3. AGENTS.md
   4. skills list
   5. discovery instructions (when used)
   6. memory: the profile and the keyword map, last, since distill changes them most often

   Cache breakpoint 1 sits at the end of the system prompt.
3. **Messages:**
   1. history
   2. the owner's message
   3. the time (`Now: …`, a role-system item)
   4. run-start reminders
   5. then the run's turns. Breakpoint 2 is the last message, so each turn reads the run so far from the cache.

**Rule:** anything per run or per turn (time, reminders, subagent context, memory changes) goes after the owner's
message or in tool results. It never goes in the system prompt or the tool list. Moving the time out of the system
prompt is what made the prefix stable: before, it changed every minute.

`context_built` and every `context_tokens` event carry a `prefix_hash` (the tool list and the system prompt). Two
calls with the same hash could share a cache entry. `runs tokens` says whether the prefix held across a run's calls.

## Per provider

- **Anthropic** caches only what's marked. `providers.model_settings` passes LiteLLM
  `cache_control_injection_points` through `ModelSettings.extra_args`:
  - `index 0`: the system prompt the Agents SDK puts first
  - `index -1`: the last message

  The first point is by index, not by role, because LiteLLM moves later system messages (the time note, a
  final-answer instruction) into Anthropic's system blocks. Marking by role would mark them too, and would cache
  on a block that changes. (The contract note had marked by role; index 0 was approved instead.) Two breakpoints
  (Anthropic allows 4). Below the model's minimum cacheable size, nothing
  is cached and nothing fails.
- **OpenAI** caches a stable prefix of 1,024+ tokens by itself. Nothing is marked.
- **Ollama and OpenAI-compatible endpoints** get no settings (nothing to mark).

## Cache tokens in the events and the budgets

- `context_tokens.cache_read` and `cache_write` per call. `runs tokens` sums them.
- LiteLLM reports Anthropic's cache writes as `cache_creation_input_tokens`, which the Agents SDK doesn't read.
  `providers.CachingLitellmModel` copies them into `cache_write_tokens`, so they aren't lost.
- **Budgets count every input token once, at full weight** (owner decision). Both OpenAI's `input_tokens` and
  LiteLLM's (Anthropic) already include cache reads and writes, so `views.tokens_used` sums input + output as
  reported, and the cache fields are never added again. A cached token is cheaper, but it still counts toward the
  per-run and daily budgets.

## Checked how

All offline, in `tests/efficiency/test_caching.py`:
- **Byte-identical prefix.** Two turns of one run, plus a second run of the same conversation an hour later: the system
  prompt is byte-identical, the tools are the same, and every prefix hash is equal. The time is after the owner's
  message.
- **Through the real Agents SDK → LiteLLM path, with `litellm==1.83.0` as locked.** The test catches the HTTP request
  LiteLLM builds for Anthropic (`httpx.AsyncClient.send` replaced, no network):
  - `cache_control` sits on the stable system block.
  - On the first call, it also sits on the last message.
  - On the second call (after a tool call), it sits on the tool result.
  - There are exactly 2 marks.
- **Usage.** Canned usage (1,500 written, 300 read) comes back as `cache_write=1500`, `cache_read=300` and
  `input_tokens=1840`, and the budget counts 1,840 once.

The owner's one paid check: run two turns on Claude and look at `local-agent runs tokens <id>`. The second call
should show a cache read about the size of the tool list plus the system prompt.
