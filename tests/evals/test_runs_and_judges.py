"""
Repeated runs (--runs N: pass k/N, mean tokens and cost) and several judges (a rubric passes only if every judge
passes it; how often they agreed is in the table). Scripted models and judges only.
"""

import json
import re

from local_agent.evals import __main__ as cli
from local_agent.evals import scripted
from local_agent.evals.runner import ModelSpec, run_evals
from local_agent.evals.scripted import ScriptedModel, say

SCRIPTED = ModelSpec('scripted', 'scripted')
JUDGE_A, JUDGE_B = ModelSpec('scripted', 'judge-a'), ModelSpec('scripted', 'judge-b')
VERDICT = '{{"pass": {}, "reason": "{}"}}'


def judges(*verdicts):
    """judge_factory: one scripted judge per spec, answering the same verdict every time."""
    by_name = dict(zip(('judge-a', 'judge-b'), verdicts, strict=False))
    return lambda spec: ScriptedModel([[say(by_name[spec.name])]] * 50)


def test_runs_repeat_each_task_and_the_cell_shows_passes_out_of_n(tmp_path, capsys):
    assert cli.main(['--scripted', '--runs', '3', '--task', 'calculation', '--out', str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert re.search(r'\| calculation \| pass 3/3 · [\d.]+k tok · \$0\.000 \|', printed)
    assert '| **total** | 3/3 · ' in printed
    assert 'passes out of 3 runs per task' in printed
    report = json.loads((tmp_path / 'report.json').read_text())
    assert report['runs'] == 3 and [r['run'] for r in report['results']] == [1, 2, 3]
    for n in (1, 2, 3):  # one home per run, kept for the inspector
        assert (tmp_path / 'runs' / 'scripted' / 'calculation' / f'run-{n}' / 'home' / 'state.db').exists()


async def test_a_flaky_model_passes_some_runs(tmp_path):
    played = []

    def flaky(spec, task_id):
        played.append(task_id)
        if len(played) % 2:  # runs 1 and 3 calculate, run 2 guesses
            return scripted.scripted_model(task_id)
        return ScriptedModel([[say('About 400.')]])

    report = await run_evals([SCRIPTED], [], tmp_path, runs=3, task_ids=['calculation'], model_factory=flaky,
                             say=lambda _: None)
    assert [r['passed'] for r in report.results] == [True, False, True]
    table = (tmp_path / 'report.md').read_text()
    assert '| calculation | pass 2/3 · ' in table
    assert '- scripted / calculation (run 2): checks: uses the calculator, right number' in table
    assert 'Judges: none (checks only).' in table


async def test_rounds_go_across_models_before_the_next_round(tmp_path):
    other = ModelSpec('scripted', 'other')
    report = await run_evals([SCRIPTED, other], [], tmp_path, runs=2, task_ids=['calculation', 'memory-recall'],
                             model_factory=lambda spec, task_id: scripted.scripted_model(task_id), say=lambda _: None)
    assert [(r['task'], r['run'], r['model']) for r in report.results] == [
        ('memory-recall', 1, 'scripted'), ('memory-recall', 1, 'scripted:other'),
        ('memory-recall', 2, 'scripted'), ('memory-recall', 2, 'scripted:other'),
        ('calculation', 1, 'scripted'), ('calculation', 1, 'scripted:other'),
        ('calculation', 2, 'scripted'), ('calculation', 2, 'scripted:other')]


async def test_both_judges_must_pass_and_their_disagreement_is_reported(tmp_path):
    report = await run_evals([SCRIPTED], [JUDGE_A, JUDGE_B], tmp_path, task_ids=['calculation'],
                             judge_factory=judges(VERDICT.format('true', 'fine'), VERDICT.format('false', 'no units')),
                             say=lambda _: None)
    [result] = report.results
    assert result['judges'] == {'scripted:judge-a': {'pass': True, 'reason': 'fine'},
                                'scripted:judge-b': {'pass': False, 'reason': 'no units'}}
    assert (result['failed_checks'], result['rubric_pass'], result['passed']) == ([], False, False)
    assert result['rubric_reason'] == 'scripted:judge-b: no units'
    table = (tmp_path / 'report.md').read_text()
    assert 'Judges: scripted:judge-a, scripted:judge-b ($0.000). A rubric passes only when every judge passes it. ' \
           'The judges agreed on 0 of 1 answers (0%).' in table
    assert 'The judges disagreed:\n- scripted / calculation: scripted:judge-a pass, scripted:judge-b fail' in table
    assert json.loads((tmp_path / 'report.json').read_text())['agreement'] == {'graded': 1, 'agreed': 0}


async def test_agreeing_judges_pass_together(tmp_path):
    report = await run_evals([SCRIPTED], [JUDGE_A, JUDGE_B], tmp_path, runs=2, task_ids=['calculation'],
                             judge_factory=judges(VERDICT.format('true', 'a'), VERDICT.format('true', 'b')),
                             say=lambda _: None)
    assert all(r['passed'] and r['rubric_pass'] for r in report.results)
    assert 'The judges agreed on 2 of 2 answers (100%).' in (tmp_path / 'report.md').read_text()


async def test_a_judge_stopped_by_the_cap_leaves_the_rubric_undecided(tmp_path):
    from agents.usage import Usage

    from local_agent.evals.meter import Price

    usage = Usage(requests=1, input_tokens=1000, output_tokens=100, total_tokens=1100)
    pricey = ModelSpec('scripted', 'pricey')
    # the model's calls fit; the second judge's estimate takes the total past the cap
    report = await run_evals([SCRIPTED], [JUDGE_A, pricey], tmp_path, max_cost=0.001, task_ids=['calculation'],
                             prices={'pricey': Price(1e-6, 1e-6)},
                             judge_factory=lambda spec: ScriptedModel([[say(VERDICT.format('true', 'ok'))]],
                                                                      usage=usage), say=lambda _: None)
    [result] = report.results
    assert result['judges']['scripted:pricey']['reason'] == 'not judged: the cap was reached'
    assert (result['rubric_pass'], result['passed']) == (None, False)
    assert report.stopped


def test_scripted_mode_uses_two_fake_judges(tmp_path, capsys):
    assert cli.main(['--scripted', '--task', 'calculation', '--out', str(tmp_path)]) == 0
    assert 'judges: scripted:judge-a, scripted:judge-b' in capsys.readouterr().out
    [result] = json.loads((tmp_path / 'report.json').read_text())['results']
    assert set(result['judges']) == {'scripted:judge-a', 'scripted:judge-b'}


def test_judge_none_skips_the_rubrics(tmp_path, capsys):
    assert cli.main(['--model', 'scripted', '--judge', 'none', '--task', 'calculation', '--out', str(tmp_path)]) == 0
    assert 'judges: none' in capsys.readouterr().out
    [result] = json.loads((tmp_path / 'report.json').read_text())['results']
    assert result['judges'] == {} and result['rubric_pass'] is None and result['passed']

