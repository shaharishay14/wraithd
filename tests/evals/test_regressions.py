"""
Each Phase 9 eval task catches the regression it's for: with its feature turned off in a throwaway copy of the
source, the task fails; on the source as it is, it passes.

Slow (a subprocess eval run per case), so it's skipped unless LOCAL_AGENT_PROVE_EVALS=1:

    LOCAL_AGENT_PROVE_EVALS=1 uv run pytest tests/evals/test_regressions.py -s

Each case turns its feature off with the smallest edit that does it (the same effect as the code before the
feature): memory writes from a tainted run no longer go to review (S2), MCP pins are ignored and a config
always-allow holds again (S3), the spill hook is not registered (E4).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import local_agent

SOURCE = Path(local_agent.__file__).parent

# task -> (file under local_agent/, text, replacement): the feature turned off
FEATURE_OFF = {
    'memory-poisoning': ('memory/tools.py', 'if review.goes_to_review(gate):', 'if False:  # S2 off'),
    'mcp-tool-changed': ('mcp/client.py', 'verdict = verdicts.get(tool.name)', 'verdict = None  # S3 off'),
    'big-page': ('core/tool_results.py', 'hooks.append(spill_hook(rt, log))', 'pass  # E4 off'),
}

pytestmark = pytest.mark.skipif(not os.environ.get('LOCAL_AGENT_PROVE_EVALS'),
                                reason='slow: set LOCAL_AGENT_PROVE_EVALS=1 to prove each task fails without its '
                                       'feature')


def _run(task_id: str, src: Path | None, out: Path) -> dict:
    env = {**os.environ, **({'PYTHONPATH': str(src)} if src else {})}
    where = subprocess.run([sys.executable, '-c', 'import local_agent; print(local_agent.__file__)'], env=env,
                           capture_output=True, text=True, check=True).stdout.strip()
    assert where.startswith(str(src or SOURCE)), where  # the copy is what runs, the world servers included
    subprocess.run([sys.executable, '-m', 'local_agent.evals', '--scripted', '--task', task_id, '--out', str(out)],
                   env=env, capture_output=True, text=True, timeout=300)
    [result] = json.loads((out / 'report.json').read_text())['results']
    return result


@pytest.mark.parametrize('task_id', list(FEATURE_OFF))
def test_the_task_fails_without_its_feature_and_passes_with_it(task_id, tmp_path):
    relative, text, replacement = FEATURE_OFF[task_id]
    copy = tmp_path / 'src'
    shutil.copytree(SOURCE, copy / 'local_agent', ignore=shutil.ignore_patterns('__pycache__', 'ui'))
    target = copy / 'local_agent' / relative
    source = target.read_text()
    assert source.count(text) == 1, f'{relative} changed: update FEATURE_OFF'
    target.write_text(source.replace(text, replacement))

    off = _run(task_id, copy, tmp_path / 'off')
    on = _run(task_id, None, tmp_path / 'on')
    print(f'\n{task_id}: feature off -> {"pass" if off["passed"] else "FAIL"} {off["failed_checks"]}; '
          f'feature on -> {"pass" if on["passed"] else "FAIL"} {on["failed_checks"]}')
    assert not off['passed'] and off['failed_checks']
    assert on['passed'] and on['failed_checks'] == []
