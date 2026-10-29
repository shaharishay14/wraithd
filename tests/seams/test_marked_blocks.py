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
    'daemon/api.py': [('# Workers import their route modules', 'logger = '),
                      ("Route('/api/telegram/pairing'", "Route('/api/{rest:path}'")],
    'doctor.py': [('def checks', 'return found')],
    'audit.py': [("'local-agent schedules list')", 'return found')],
    'core/tool_results.py': [('hooks: list[Hook] = []', 'return hooks')],
    'store/modules.py': [('MIGRATIONS: list[Migration] = [', ']')],
    'daemon/jobs.py': [("found = [Job('distill'", 'return found')],
}

MARKER = re.compile(r'^\s*# (>>>|<<<) (\w+)\s*$')


def check_blocks(path: str, lines: list[str], anchors: list[tuple[str, str]]) -> None:
    markers = [(number, *MARKER.match(line).groups()) for number, line in enumerate(lines) if MARKER.match(line)]
    expected = [(side, worker) for _ in anchors for worker in WORKERS for side in ('>>>', '<<<')]
    assert [(side, worker) for _, side, worker in markers] == expected

    # Each registry's blocks sit after its anchor and before the next line that closes it
    groups = [markers[i:i + 2 * len(WORKERS)] for i in range(0, len(markers), 2 * len(WORKERS))]
    for (after, before), group in zip(anchors, groups, strict=True):
        first, last = group[0][0], group[-1][0]
        start = max(i for i in range(first) if after in lines[i])
        assert start < first
        end = next(i for i in range(last + 1, len(lines)) if lines[i].strip().startswith(before))
        # the blocks are one run of lines: outside a block, only markers (a block's own lines are the worker's)
        inside = False
        for i in range(first, last + 1):
            match = MARKER.match(lines[i])
            if match:
                inside = match.group(1) == '>>>'
            else:
                assert inside, f'{path}: blocks interleaved with code'
        assert end > last


@pytest.mark.parametrize('path', sorted(REGISTRIES))
def test_each_registry_has_every_workers_block(path):
    check_blocks(path, (SRC / path).read_text().splitlines(), REGISTRIES[path])


def _registry(between: list[str] = (), inside_first: list[str] = ()) -> list[str]:
    blocks = [f'    # {side} {worker}' for worker in WORKERS for side in ('>>>', '<<<')]
    return ['x = {', blocks[0], *inside_first, blocks[1], *between, *blocks[2:], '}']


def test_a_block_with_content_passes():
    check_blocks('x.py', _registry(inside_first=["    'a': 1,"]), [('x = {', '}')])


def test_code_between_blocks_fails():
    with pytest.raises(AssertionError, match='interleaved'):
        check_blocks('x.py', _registry(between=["    'a': 1,"]), [('x = {', '}')])


def test_between_runs_still_holds_the_core_types():
    # workers add their own between-runs types in their blocks, after the core ones
    assert ev.BETWEEN_RUNS[:3] == (ev.COMPACTION, ev.DISTILL, ev.TELEGRAM_IGNORED)



def test_the_api_blocks_come_before_the_catch_all():
    text = (SRC / 'daemon/api.py').read_text()
    assert text.index('# <<< sandbox') < text.index("Route('/api/{rest:path}'")
