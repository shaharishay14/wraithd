"""
Where a run's input tokens go: the system prompt, AGENTS.md, the profile, the tool list, the skills list, the
history, tool results, the harness's notes.

TokenMeter wraps the run's model and sees each call exactly as it's sent. It counts each part with one tokenizer
(tiktoken o200k, the files LiteLLM bundles, so it works offline) and logs a context_tokens event with the counts,
the input tokens the provider reported, and the cache reads and writes. `local-agent runs tokens <id>` prints it.

The counts are the harness's own measurement: for OpenAI models they match the provider's tokenizer, for Claude
and local models they are close but not exact (another tokenizer). What's left between the measured parts and
the reported total is message framing, shown as its own row.
"""

import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path

from agents import FunctionTool, Model

from local_agent.core import events as ev

logger = logging.getLogger(__name__)

# The parts of a model call's input, in the order they're sent (see docs/caching.md for why this order)
TOOLS = 'tools'
SYSTEM = 'system'  # the base instructions, the reminders preamble, discovery instructions
AGENTS_MD = 'agents_md'
SKILLS = 'skills'
PROFILE = 'profile'  # memory: the profile (L0) and the keyword map (L1)
HISTORY = 'history'  # earlier messages, the owner's message, the model's own messages and tool calls
TOOL_RESULTS = 'tool_results'  # with the reminders added after them
NOTES = 'notes'  # the harness's notes: the time, run-start reminders, a final-answer instruction
PARTS = (TOOLS, SYSTEM, AGENTS_MD, SKILLS, PROFILE, HISTORY, TOOL_RESULTS, NOTES)
SYSTEM_SECTIONS = (AGENTS_MD, SKILLS, PROFILE)  # system-prompt parts the run names; the rest counts as SYSTEM

NOTE_PREFIX = '<system-reminder>'  # a message that is the harness's own note (reminders.TAG)
ENCODING = 'o200k_base'
_encoder = None  # the tiktoken encoding, loaded once; False when it can't be loaded


def _litellm_tokenizers() -> str | None:
    """LiteLLM ships tiktoken's encoding files, so counting needs no download."""
    spec = importlib.util.find_spec('litellm')
    if not spec or not spec.submodule_search_locations:
        return None
    folder = Path(next(iter(spec.submodule_search_locations))) / 'litellm_core_utils' / 'tokenizers'
    return str(folder) if folder.is_dir() else None


def _get_encoder():
    global _encoder
    if _encoder is None:
        try:
            folder = _litellm_tokenizers()
            if folder:
                os.environ.setdefault('TIKTOKEN_CACHE_DIR', folder)
            import tiktoken

            _encoder = tiktoken.get_encoding(ENCODING)
        except Exception as e:
            logger.warning(f'No tokenizer ({type(e).__name__}); token counts are estimated as characters / 4')
            _encoder = False
    return _encoder


def tokenizer_name() -> str:
    return ENCODING if _get_encoder() else 'chars/4'


def count(text: str) -> int:
    """Tokens in one text, by the meter's tokenizer (characters / 4 if it can't be loaded)."""
    if not text:
        return 0
    encoder = _get_encoder()
    if not encoder:
        return (len(text) + 3) // 4
    return len(encoder.encode(text, disallowed_special=()))


def tool_schema(tool) -> str:
    """One tool as the model gets it: name, description, parameters."""
    if isinstance(tool, FunctionTool):
        return json.dumps({'name': tool.name, 'description': tool.description,
                           'parameters': tool.params_json_schema}, sort_keys=False)
    return json.dumps({'name': getattr(tool, 'name', type(tool).__name__)})


def prefix_hash(instructions: str, tools) -> str:
    """
    A short hash of what a provider caches: the tool list and the system prompt. Equal hashes on two model calls
    mean the cached prefix could be reused; a change shows which run broke the cache.
    """
    text = '\n'.join(tool_schema(tool) for tool in tools) + '\n\0\n' + (instructions or '')
    return hashlib.sha256(text.encode()).hexdigest()[:12]


_SKIPPED_KEYS = {'id', 'call_id', 'type', 'status', 'role', 'detail', 'annotations'}


def _texts(value) -> list[str]:
    """The strings a model reads in an input item (not ids or type tags)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() if key not in _SKIPPED_KEYS
                and not (key == 'image_url' and isinstance(item, str) and item.startswith('data:'))
                for text in _texts(item)]
    if isinstance(value, list | tuple):
        return [text for item in value for text in _texts(item)]
    return []


def part_of(item) -> str:
    """Which part of the input one item is: a tool result, a harness note, or history."""
    if not isinstance(item, dict):
        return HISTORY
    if item.get('type') == 'function_call_output':
        return TOOL_RESULTS
    if item.get('role') == 'system':
        return NOTES
    content = item.get('content')
    if item.get('role') == 'user' and isinstance(content, str) and content.startswith(NOTE_PREFIX):
        return NOTES
    return HISTORY


class _Counter:
    """count() with a memory: the system prompt, the tools and earlier items are the same on every call."""

    def __init__(self):
        self.seen: dict[str, int] = {}

    def __call__(self, text: str) -> int:
        if text not in self.seen:
            if len(self.seen) > 2000:
                self.seen.clear()
            self.seen[text] = count(text)
        return self.seen[text]


def measure(system_instructions: str | None, input_items, tools, sections: dict[str, str] | None = None,
            counter=count) -> dict[str, int]:
    """Tokens of each part of one model call's input (PARTS). sections: system-prompt parts by name, as sent."""
    parts = dict.fromkeys(PARTS, 0)
    parts[TOOLS] = sum(counter(tool_schema(tool)) for tool in tools or [])
    system = system_instructions or ''
    named = 0
    for name, text in (sections or {}).items():
        if text and text in system:
            parts[name] = counter(text)
            named += parts[name]
    parts[SYSTEM] = max(counter(system) - named, 0)
    items = [{'role': 'user', 'content': input_items}] if isinstance(input_items, str) else input_items or []
    for item in items:
        if hasattr(item, 'model_dump'):
            item = item.model_dump()
        parts[part_of(item)] += sum(counter(text) for text in _texts(item))
    return parts


def _usage_numbers(usage) -> dict[str, int]:
    """Input tokens as reported, and the cache reads and writes inside them (0 when not reported)."""
    details = getattr(usage, 'input_tokens_details', None)
    if isinstance(details, list):  # some SDK versions keep one entry per request
        details = details[0] if details else None
    return {'input_tokens': getattr(usage, 'input_tokens', 0) or 0,
            'cache_read': getattr(details, 'cached_tokens', 0) or 0,
            'cache_write': getattr(details, 'cache_write_tokens', 0) or 0}


class TokenMeter(Model):
    """
    The run's model, measured: every call is passed on unchanged, and a context_tokens event records what each part
    of its input cost and what the provider reported. A failure to measure never fails the call.
    """

    def __init__(self, model: Model, log: ev.EventLog, sections: dict[str, str] | None = None):
        self.inner, self.log, self.sections = model, log, dict(sections or {})
        self.model = getattr(model, 'model', model)  # what the event log names as the model
        self._count = _Counter()

    def _measured(self, system_instructions, input_items, tools) -> dict:
        try:
            parts = measure(system_instructions, input_items, tools, self.sections, self._count)
            return {'parts': parts, 'measured': sum(parts.values()),
                    'prefix_hash': prefix_hash(system_instructions or '', tools or [])}
        except Exception as e:
            logger.warning(f'Could not measure a model call: {type(e).__name__}: {e}')
            return {}

    def _record(self, measured: dict, usage) -> None:
        self.log.append(ev.CONTEXT_TOKENS, model=str(self.model), tokenizer=tokenizer_name(), **measured,
                        **_usage_numbers(usage))

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing,
                           **kwargs):
        measured = self._measured(system_instructions, input, tools)
        response = await self.inner.get_response(system_instructions, input, model_settings, tools, output_schema,
                                                 handoffs, tracing, **kwargs)
        self._record(measured, getattr(response, 'usage', None))
        return response

    async def stream_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                              tracing, **kwargs):
        measured = self._measured(system_instructions, input, tools)
        async for event in self.inner.stream_response(system_instructions, input, model_settings, tools,
                                                      output_schema, handoffs, tracing, **kwargs):
            if getattr(event, 'type', None) == 'response.completed':
                self._record(measured, getattr(event.response, 'usage', None))
            yield event

    def get_retry_advice(self, request):
        return self.inner.get_retry_advice(request)

    async def close(self) -> None:
        await self.inner.close()


# --- the breakdown: `local-agent runs tokens <id>` --------------------------------------------------------------

LABELS = {TOOLS: 'tool list', SYSTEM: 'system prompt', AGENTS_MD: 'AGENTS.md', SKILLS: 'skills list',
          PROFILE: 'profile + memory map', HISTORY: 'history', TOOL_RESULTS: 'tool results',
          NOTES: 'time + reminders', 'framing': 'message framing (reported - measured)'}
COLUMNS = {TOOLS: 'tools', SYSTEM: 'system', AGENTS_MD: 'AGENTS.md', SKILLS: 'skills', PROFILE: 'profile',
           HISTORY: 'history', TOOL_RESULTS: 'results', NOTES: 'notes'}


def breakdown(events) -> dict:
    """
    One run's input tokens by part: each model call, and the run's totals. Totals add the parts of every call, so a
    tool list sent on five calls counts five times, as the provider bills it.
    """
    calls = []
    for event in events:
        if event.type != ev.CONTEXT_TOKENS:
            continue
        payload = event.payload
        parts = {name: payload.get('parts', {}).get(name, 0) for name in PARTS}
        reported = payload.get('input_tokens', 0)
        calls.append({'parts': parts, 'measured': payload.get('measured', sum(parts.values())),
                      'reported': reported, 'cache_read': payload.get('cache_read', 0),
                      'cache_write': payload.get('cache_write', 0), 'prefix_hash': payload.get('prefix_hash'),
                      'model': payload.get('model'), 'tokenizer': payload.get('tokenizer')})
    totals = {name: sum(call['parts'][name] for call in calls) for name in PARTS}
    summed = {key: sum(call[key] for call in calls) for key in ('measured', 'reported', 'cache_read', 'cache_write')}
    return {'calls': calls, 'parts': totals, **summed,
            'prefix_hashes': list(dict.fromkeys(call['prefix_hash'] for call in calls if call['prefix_hash'])),
            'tokenizer': next((call['tokenizer'] for call in calls if call['tokenizer']), None)}


def _row(label: str, tokens: int, total: int) -> str:
    share = f'{100 * tokens / total:5.1f}%' if total else '     -'
    return f'  {label:<40} {tokens:>9,}  {share}'


def render_text(run_id: str, found: dict) -> str:
    if not found['calls']:
        return f'Run {run_id}: no measured model calls (runs before token metering have none).'
    calls = found['calls']
    reported, measured = found['reported'], found['measured']
    total = reported or measured
    lines = [f'Run {run_id}: {len(calls)} model call{"s" if len(calls) != 1 else ""}, '
             f'{reported:,} input tokens reported ({found["tokenizer"]} count: {measured:,})', '']
    for name in PARTS:
        lines.append(_row(LABELS[name], found['parts'][name], total))
    if reported:
        lines.append(_row(LABELS['framing'], reported - measured, total))
    lines.append('')
    lines.append(f'  cache: {found["cache_read"]:,} read, {found["cache_write"]:,} written'
                 + (f' ({100 * found["cache_read"] / reported:.0f}% of input from cache)' if reported else ''))
    hashes = found['prefix_hashes']
    lines.append('  prefix: ' + ('the same on every call' if len(hashes) == 1 else
                                 f'{len(hashes)} different prefixes (the cache restarted)'))
    lines.append('')
    lines.append('  call' + ''.join(f'{COLUMNS[name]:>10}' for name in PARTS) + '  reported    cached')
    for number, call in enumerate(calls, 1):
        lines.append(f'  {number:>4}' + ''.join(f'{call["parts"][name]:>10,}' for name in PARTS)
                     + f'{call["reported"]:>10,}{call["cache_read"]:>10,}')
    return '\n'.join(lines)


def main(home, prefix: str, as_json: bool) -> int:
    """`local-agent runs tokens <id>`."""
    import sys

    from local_agent.store import SQLiteStore

    store = SQLiteStore(home.db_path) if home.db_path.exists() else None
    try:
        matches = store.find_runs(prefix) if store and prefix else []
        if len(matches) != 1:
            print(f'No run matches {prefix!r}.' if not matches else
                  f'{len(matches)} runs start with {prefix!r}; give more of the id.', file=sys.stderr)
            return 1
        found = breakdown(store.run_events(matches[0]))
    finally:
        if store:
            store.close()
    print(json.dumps({'run_id': matches[0], **found}, indent=1) if as_json else render_text(matches[0], found))
    return 0
