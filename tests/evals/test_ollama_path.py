"""
The local-model column, end to end without Ollama: an OpenAI-compatible fake server on 127.0.0.1 (fake_openai.py)
plays the golden scripts as Chat Completions, and `--model ollama:NAME --ollama-url URL` runs the whole set through
the setup check, providers.build_model (Chat Completions, no key), the real run(), the world's MCP servers and the
graders. The owner runs the real column against a real Ollama.
"""

import json
import re

from local_agent.evals import __main__ as cli
from local_agent.evals.runner import available
from local_agent.evals.tasks import TASKS
from tests.evals.fake_openai import FakeOpenAI


def test_an_openai_compatible_local_model_runs_the_whole_set(tmp_path, capsys):
    with FakeOpenAI() as server:
        code = cli.main(['--model', 'ollama:fake-qwen', '--ollama-url', server.url, '--judge', 'scripted:judge-a',
                         '--out', str(tmp_path)])
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert '| task | ollama:fake-qwen |' in printed
    runnable = [task['id'] for task in TASKS if not available(task)]
    assert re.search(rf'\| \*\*total\*\* \| {len(runnable)}/{len(runnable)} · [\d.]+k tok · \$0\.000 \|', printed), \
        printed
    report = json.loads((tmp_path / 'report.json').read_text())
    assert [r['task'] for r in report['results'] if r['passed']] == runnable

    # it really went over HTTP as Chat Completions, with the harness's tools, and named the model
    assert server.requests and all(body['model'] == 'fake-qwen' for body in server.requests)
    # streamed, asking for usage, so the budgets and the table count a local model's tokens
    assert all(body.get('stream') and body['stream_options'] == {'include_usage': True} for body in server.requests)
    offered = {tool['function']['name'] for body in server.requests for tool in body.get('tools', [])}
    assert {'GMAIL_FETCH_EMAILS', 'CALCULATOR_CALCULATE', 'find_app_actions'} <= offered
    # the usage the server reported reached the event log (the table's tokens)
    assert all(r['tokens'] > 0 for r in report['results'])


def test_the_setup_check_names_a_local_server_that_is_not_answering(tmp_path, capsys):
    with FakeOpenAI() as server:
        url = server.url
    assert cli.main(['--model', 'ollama:fake-qwen', '--ollama-url', url, '--judge', 'none',
                     '--out', str(tmp_path)]) == 2
    assert f'Ollama isn\'t answering at {url}' in capsys.readouterr().err
