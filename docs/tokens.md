# Tokens

**Files:** `core/tokens.py`, `run.py`, `cli.py` (efficiency block)

## One-liner

> Every model call is measured part by part as it's sent, so "where did the tokens go?" has an answer in the event
> log: `local-agent runs tokens <id>`.

## How it works

- `run()` wraps the run's model in a `TokenMeter`. It passes every call through unchanged and sees each one exactly
  as it's sent: the system prompt, the input items, the tool list.
- **One `context_tokens` event per model call** (just before that call's `model_output`). It holds:
  - `parts`: the tokens of each part:

    | Part | What it counts |
    |---|---|
    | `tools` | the tool list |
    | `system` | base instructions, the reminders preamble, discovery instructions |
    | `agents_md` | the standing orders |
    | `skills` | the skills list |
    | `profile` | memory: the profile (L0) and the keyword map (L1) |
    | `history` | earlier messages, the owner's message, the model's own messages and tool calls |
    | `tool_results` | tool results, with the reminders added after them |
    | `notes` | the time, run-start reminders, a final-answer instruction |

  - `measured`: their sum.
  - `input_tokens`: what the provider reported.
  - `cache_read`, `cache_write` ([caching](caching.md)).
  - `prefix_hash` and `tokenizer`.
- **How parts are counted.** `run.py` hands the meter the system-prompt sections it built (AGENTS.md, skills,
  profile). Each section is counted on its own, and the rest of the system prompt counts as `system`.
- **The tokenizer** is tiktoken's `o200k_base`, loaded from the encoding files LiteLLM ships, so it works offline. If
  it can't load, counts fall back to characters / 4 and the event says `chars/4`.
- **Message framing.** The provider counts a few tokens around each message and tool that are in no part. The
  breakdown shows them as their own row: reported minus measured.
- **Measuring never breaks a run.** A failure is logged as a warning, and the event then carries only the reported
  usage.

```
$ local-agent runs tokens 3f2a
Run 3f2a…: 2 model calls, 6,077 input tokens reported (o200k_base count: 5,773)

  tool list                                    4,264   70.2%
  system prompt                                  190    3.1%
  AGENTS.md                                       64    1.1%
  skills list                                    354    5.8%
  profile + memory map                           322    5.3%
  history                                         25    0.4%
  tool results                                   520    8.6%
  time + reminders                                34    0.6%
  message framing (reported - measured)          304    5.0%
```

`--json` gives the same data, with one entry per call.

## Checked how

- `tests/efficiency/runs/` holds two recorded runs, made with `tests/efficiency/record_runs.py`. The model there
  reports input tokens the way a provider does: it counts the request it was sent with its own serialization and
  framing, independently of the meter.
- On every call of both runs, the parts add up to the reported input tokens within 7%. The gap is message framing.
- `tests/core/runs/*.json` predate the meter and report 0 tokens, so they have no breakdown.

## What's honest to say

- **The counts are exact only for OpenAI's tokenizer.** Claude and local models tokenize differently. The parts'
  shares are still right, but their absolute sizes are off by the tokenizer difference, which the framing row
  absorbs.
- **The Claude-vs-gpt-4o input gap isn't explained yet.** The 2026-10-26 eval logs aren't available, and the runs here
  are scripted. The next paid eval run records `context_tokens` on both models. `runs tokens` on a pair of runs
  will then say which part differs (most likely the tool list and framing, since Anthropic's tool format is
  wordier).
- **The tool list is the largest part.** On the recorded runs it's about 70% of every call's input, ahead of the
  system prompt, and it's sent on every call. Caching ([caching](caching.md)) makes it cheap after the first call.
  Progressive disclosure ([disclosure](progressive-disclosure.md)) is what makes it smaller.
