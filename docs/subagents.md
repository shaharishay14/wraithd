# Subagents

**Files:** `core/delegate.py`, `core/views.py` (`delegated_tokens`), `core/stop.py`, `core/inspector.py`

## One-liner

> `delegate(task)` hands one self-contained task to a child run with a context of its own, and gets back a short
> summary instead of every page the child read. The budget, the taint and the approvals carry over; nothing loosens.

## How it works

- **Off by default:** `[subagents] enabled = true` turns it on (`default_tokens = 50000`). Off, every run's tool list
  and its cached prompt prefix are exactly what they were. Only the owner's top-level runs get it.
- **The child** is a normal `run()`: `source='subagent'`, `purpose='subagent'` (its model is
  `rt.model_for('subagent')`), `parent_run_id` on its `run_start`. Its first user message is the task, so the
  system prompt and tool list stay the stable prefix. It gets the parent's tools (the built-ins already chosen for
  attended or not, the same app connections) and its own memory and skill tools.
- **Summary, not transcript:** the tool returns the child's final answer (at most 4,000 characters), its run id,
  outcome and tokens.
- **Budget:** the child gets at most what's left of the parent's run budget (and of the daily one). Its tokens are
  recorded in the parent's `subagent_result` event and count toward the parent's run budget
  (`views.delegated_tokens`, which `StopPolicy` adds). The daily budget counts each run's own `run_end` tokens, so
  the child's are counted once. The parent's run time limit (180 s) includes the child.
- **Taint both ways:** the child starts with everything third-party the parent read (`inherited_taint`). When the
  parent was tainted, the task is the model's words after reading someone else's, so the child's gate doesn't count
  it as the owner's (`delegated task`): addresses in it aren't the owner's recipients. A child that read third-party
  content returns `"untrusted": true`, which taints the parent. A delegate result also counts as private data.
- **Depth 1:** a child is never offered `delegate`.
- **Unattended stays unattended:** no callback for the parent, none for the child. In a conversation the child's
  approval cards reach the owner, but nothing it writes streams into the chat.
- **`runs show`** prints the child's timeline under the parent's `subagent_result`, and a child says whose subagent
  it is. `runs show --json` and the API's run detail have `parent_run_id`, `subagents` and `subagent_tokens`.

## On a laptop

Every token is the owner's own money, so a subagent never gets a budget of its own: it spends the parent's.
