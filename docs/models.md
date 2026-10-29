# Model per purpose

**Files:** `providers.py`, `config.py` (efficiency block), `doctor.py` (efficiency block)

## One-liner

> Each kind of model call can use its own model: the background work (distill, compaction, the heartbeat,
> subagents) can run on a cheaper one, and chat stays as it is.

## How it works

```toml
[models]
distill = "gpt-4.1-nano"     # memory from conversations
compaction = "gpt-4.1-nano"  # summaries of long chats
heartbeat = ""               # "" (or left out): [model] default, as before
subagent = ""
```

- The names are models of the `[model]` provider. `chat` uses `[model] default`, and `strong` uses `[model] strong`.
- `build_models` builds one model per configured name: two purposes naming the same model share one instance.
  `PURPOSE_MODEL` maps each purpose to its own key.
- `model_for` falls back to the default model when a purpose's key is missing, so an unset purpose behaves exactly
  as before. The call sites didn't change: they already ask for a purpose (see [seams](seams.md)).
- **`doctor`** makes one request for the provider's model list, with the owner's key, timing out after 5 seconds. It
  prints one line per configured model: ok if listed, FAIL if not, with the fix (another name in `[model]` or
  `[models]`). With no key, no answer or an odd answer, it prints one info line and never fails.

## Checked how

`tests/efficiency/test_models.py`:
- Unset, every purpose gets exactly the model it got before.
- Each purpose set alone changes only that purpose.
- Distill (the daemon job), compaction (`history_for`), and heartbeat and subagent runs (`run(purpose=…)`) each call
  their own model.
- Doctor's ok, FAIL and info cases, with a fake model list.
