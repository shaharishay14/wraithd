"""
Every shared registry has one marked block per worker, in the same order, where the brief says. A worker edits only
inside its own block (`# >>> <worker>` ... `# <<< <worker>`).
"""

import re
from pathlib import Path

import pytest

from local_agent.core import events as ev

SRC = Path(__file__).resolve().parents[2] / 'src' / 'local_agent'
WORKERS = ('security', 'efficiency', 'capabilities', 'reach', 'evals', 'sandbox')

# file -> (anchor the blocks follow, anchor they come before), one entry per registry in that file
REGISTRIES = {
    'tools/__init__.py': [('def builtin_tools', 'return tools')],
    'run.py': [("own += [propose_tool, adjust_tool]", 'result_hooks = tool_results.result_hooks')],
    'core/gate.py': [('BUILTIN_TOOL_ACCESS = {', '}'), ('NEVER_UNATTENDED = {', '}'), ('APPROVAL_REASONS = {', '}'),
                     ('PRIVATE_OUTPUT_BUILTINS = {', '}'), ('UNTRUSTED_OUTPUT_BUILTINS = {', '}')],
    'cli.py': [('def build_parser', 'return parser'), ('def main', 'parser.print_help()')],
    'config.py': [('SECTIONS: dict[str, Section] = {', '}')],
    'core/events.py': [('RUN_END = ', 'BETWEEN_RUNS = ('), ('BETWEEN_RUNS = (', ')')],
    'daemon/api.py': [("Route('/api/telegram/pairing'", "Route('/api/{rest:path}'")],
    'doctor.py': [('def checks', 'return found')],
    'audit.py': [("'local-agent schedules list')", 'return found')],
    'core/tool_results.py': [('hooks: list[Hook] = []', 'return hooks')],
    'store/modules.py': [('MIGRATIONS: list[Migration] = [', ']')],
    'daemon/jobs.py': [("found = [Job('distill'", 'return found')],
}

MARKER = re.compile(r'^\s*# (>>>|<<<) (\w+)\s*$')


@pytest.mark.parametrize('path', sorted(REGISTRIES))
def test_each_registry_has_every_workers_block(path):
    lines = (SRC / path).read_text().splitlines()
    markers = [(number, *MARKER.match(line).groups()) for number, line in enumerate(lines) if MARKER.match(line)]
    expected = [(side, worker) for _ in REGISTRIES[path] for worker in WORKERS for side in ('>>>', '<<<')]
    assert [(side, worker) for _, side, worker in markers] == expected

    # Each registry's blocks sit after its anchor and before the next line that closes it
    groups = [markers[i:i + 2 * len(WORKERS)] for i in range(0, len(markers), 2 * len(WORKERS))]
    for (after, before), group in zip(REGISTRIES[path], groups, strict=True):
        first, last = group[0][0], group[-1][0]
        start = max(i for i in range(first) if after in lines[i])
        assert start < first
        end = next(i for i in range(last + 1, len(lines)) if lines[i].strip().startswith(before))
        # nothing but other blocks between the anchors and the blocks' ends: the blocks are one run of lines
        assert all(MARKER.match(lines[i]) for i in range(first, last + 1)), f'{path}: blocks interleaved with code'
        assert end > last


def test_between_runs_still_holds_the_core_types():
    assert ev.BETWEEN_RUNS == (ev.COMPACTION, ev.DISTILL, ev.TELEGRAM_IGNORED)


def test_the_api_blocks_come_before_the_catch_all():
    text = (SRC / 'daemon/api.py').read_text()
    assert text.index('# <<< sandbox') < text.index("Route('/api/{rest:path}'")
