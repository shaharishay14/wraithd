# Seams

**Files:** `providers.py`, `run.py`, `core/tool_results.py`, `store/modules.py`, `daemon/jobs.py`, and a marked
block in each shared registry

## One-liner

> The places a module plugs into the harness without editing anyone else's code: it adds its own files, plus
> lines inside its own marked block in a shared registry.

## Marked blocks

Every shared registry has one block per worker, always in this order: security, efficiency, capabilities, reach,
evals, sandbox.

```python
    # >>> capabilities
    tools += browser_tools(rt)
    # <<< capabilities
```

A worker edits only inside its own block, and never moves, renames or deletes a marker.
`tests/seams/test_marked_blocks.py` checks that every block is there, in order, and in the right place.

| Registry | Where | What goes in a block |
|---|---|---|
| Built-in tools | `tools/__init__.py` `builtin_tools()` | `tools += my_tools(rt)` (rt, owner, attended in scope) |
| Run tools | `run.py` `_run()`, after the memory and skill tools | `own += [my_tool(rt, gate, log)]`, for tools that need the run's gate or event log |
| Gate tables | `core/gate.py` `BUILTIN_TOOL_ACCESS`, `NEVER_UNATTENDED`, `APPROVAL_REASONS`, `PRIVATE_OUTPUT_BUILTINS`, `UNTRUSTED_OUTPUT_BUILTINS` | one entry per new tool |
| CLI | `cli.py` `build_parser()` and `main()` | `my_cli.add_parser(commands)` / `if args.command == 'review': return my_cli.main(home, args)` |
| Config sections | `config.py` `SECTIONS` | `'heartbeat': Section({...defaults}, check)` |
| Event types | `core/events.py`, plus the `BETWEEN_RUNS` tuple | `REVIEW_QUEUED = 'review_queued'`, and the name in `BETWEEN_RUNS` if it belongs to no run |
| API routes | `daemon/api.py` `routes`, before the `/api/{rest}` catch-all | `*my_api.routes(daemon, handler),` |
| Doctor | `doctor.py` `checks()`, at the end | `found += my_checks(home, config)` (a list of `doctor.Check`) |
| Audit | `audit.py` `checks()`, at the end | `found += my_rules(home, config)` |
| Tool-result hooks | `core/tool_results.py` `result_hooks()` | `hooks.append(spill_hook(rt, log))` |
| Store migrations | `store/modules.py` `MIGRATIONS` | `Migration('security_001', 'CREATE TABLE security_reviews (...)')` |
| Periodic jobs | `daemon/jobs.py` `jobs()` | `found.append(Job('heartbeat', 1800, heartbeat))` |

Three of these files are new and have no owner (`core/tool_results.py`, `store/modules.py`, `daemon/jobs.py`).
As in every shared registry, workers edit only inside their own blocks. Two gate tables go beyond the four the
brief lists: `PRIVATE_OUTPUT_BUILTINS` and `UNTRUSTED_OUTPUT_BUILTINS`. A new tool that reads the owner's data, or
returns third-party text, must be added to them, or taint tracking won't see it.

## Model per purpose

Every model call asks for a purpose: `providers.model_for(models, purpose)`, or on a Runtime, `rt.model_for(purpose)`.
The purposes are `chat`, `strong`, `distill`, `compaction`, `heartbeat` and `subagent`. Today each one maps to the
model it always used (`PURPOSE_MODEL`). Every purpose except `strong` uses the default model. `strong` uses the
strong model, or the default one when no strong model is configured (and then the skill upgrade has nothing to
switch to). The efficiency worker's `[models]` config changes only `PURPOSE_MODEL` and `build_models`.

```python
summary = await rt.complete(prompt, 'compaction')
history = await compaction.history_for(rt.store, conversation_id, rt.completer('compaction'))
```

`evals/runner.py` builds its own model per eval spec and doesn't use this.

## run() options for a run started by another run

All of these are off by default, so existing callers don't change:

| Option | What it does |
|---|---|
| `parent_run_id` | Recorded on `run_start` (always present, `null` by default) |
| `inherited_taint` | `(source, text)` pairs of third-party content the parent read. They taint the run from its first step, and their sources are listed in `run_start.untrusted_inputs` |
| `budget` | `{'run_tokens', 'run_tool_calls', 'daily_tokens'}`. Each limit is `min(override, configured)`: an override only tightens, and never raises the machine's daily budget |
| `purpose` | Which model runs it (`model_for`). Recorded on `run_start` when it isn't `chat` |
| `allowed_tools` | The names of the only tools the model is offered, app actions included. When discovery is used, its three tools stay and search only the allowed actions |

```python
result = await run(rt, task, parent_run_id=parent.run_id, purpose='subagent', allowed_tools={'read_website'},
                   inherited_taint=[('read_website', page_text)], budget={'run_tokens': 20_000})
```

## Tool-result hook

`guard_function_tools` sends the result of every tool that ran through the run's hooks
(`tool_results.result_hooks(rt, log)`). This happens after secrets are redacted and the result is logged, and
before the result is capped, wrapped as untrusted and given reminders. A hook is `hook(tool_name, text, untrusted)`
and returns the text the model gets instead.

```python
def spill_hook(rt, log):
    def spill(tool, text, untrusted):
        if len(text) < 20_000:
            return text
        path = save(rt, log.run_id, text)
        log.append(SPILLED, tool=tool, chars=len(text), path=str(path))
        return f'[{len(text)} chars saved to {path}; read them with files_read]'
    return spill
```

- Hooks run in block order, each one on the previous hook's text.
- A hook that raises, or returns something other than a string, is skipped with a warning. It never breaks the
  tool call.
- Taint survives a hook. The gate tracks the full text, so links and addresses in it still count as third-party,
  and it wraps the hook's text as untrusted when the result was untrusted.
- Denials and precheck answers don't pass through the hooks.

## Config sections

A module's settings live in a `[table]` of their own, merged over defaults:

```python
SECTIONS = {
    # >>> capabilities
    'heartbeat': Section({'enabled': False, 'every_minutes': 30, 'chat_id': None}, check_heartbeat),
    # <<< capabilities
}
rt.config.section('heartbeat')['every_minutes']
```

- A value must have its default's type. A bool is never accepted as a number, and an int is accepted for a float.
  A default of `None` accepts any value.
- `check(values)` may raise `ConfigError` to validate anything else.
- Unknown keys are ignored, with one warning per process.
- A section can't use a core table's name (`CORE_TABLES`).

## A module's own tables

```python
MIGRATIONS = [
    # >>> security
    Migration('security_001', 'CREATE TABLE security_reviews (id INTEGER PRIMARY KEY, text TEXT NOT NULL)'),
    # <<< security
]

class Reviews(ModuleTables):
    def add(self, text):
        return self.execute('INSERT INTO security_reviews (text) VALUES (?)', (text,)).lastrowid
```

- Migration ids are `<module>_NNN`, and every table a migration creates is named `<module>_...`. Both are checked
  when the store opens.
- `SQLiteStore` applies the migrations that haven't run yet, in order. Each one runs in a single transaction
  together with its row in `schema_migrations`, so a failed migration leaves nothing behind.
- Never edit a migration that has shipped: add the next one.
- `ModuleTables` runs queries through `Store.transaction()`, which holds the store's lock and commits on success.
  The core schema and `store/base.py` stay untouched.

## Periodic jobs

`daemon/jobs.py` lists the daemon's periodic jobs, each a `Job(name, every_seconds, run)` where `run` is
`async run(rt)`. Background distill is the first job (hourly). The daemon runs each job in its own loop: wait,
run, repeat. A job that fails is logged as a warning and runs again on the next round. A job that's off can simply
be left out of `jobs()` based on `rt.config.section(...)`.

```python
    # >>> capabilities
    if rt.config.section('heartbeat')['enabled']:
        found.append(Job('heartbeat', rt.config.section('heartbeat')['every_minutes'] * 60, heartbeat))
    # <<< capabilities
```

`distill_all` and `DISTILL_EVERY_SECONDS` moved here from `daemon/app.py`, which still re-exports them.

## Events

There are no new event types. `run_start` gets `parent_run_id` (always present) and `purpose` (only when it isn't
`chat`), and inherited taint is listed in its `untrusted_inputs`. On a scripted run, the event log before and
after these seams differs only by `"parent_run_id": null` on `run_start`.
