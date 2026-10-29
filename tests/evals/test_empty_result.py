"""
The empty-result reminder (core/reminders.py): a search that finds nothing gets a nudge to broaden the query before
the model says something doesn't exist. Replays the gpt-4o secret-in-email failure: it searched
from:support@cloudhost.com, found nothing and gave up; the sender was support@cloudhost.io.
"""

import json

from local_agent.core import events as ev
from local_agent.core import reminders
from local_agent.evals import world
from local_agent.evals.runner import run_task
from local_agent.evals.scripted import ScriptedModel, call, say
from local_agent.evals.tasks import TASKS, failed_checks

SECRET = next(task for task in TASKS if task['id'] == 'secret-in-email')
WRONG_DOMAIN = {'query': 'from:support@cloudhost.com'}


def _fired(tool, output, arguments=None, events=None) -> list[str]:
    calls = [ev.Event(0, ev.TOOL_CALL, {'tool': tool, 'arguments': arguments or {}})] if events is None else events
    situation = reminders.Situation(message='', events=calls, tool=tool, tool_output=output)
    return [reminder.name for reminder, _ in reminders.due(reminders.REMINDERS, tool, situation)]


def test_an_empty_search_gets_the_reminder():
    assert 'empty_result' in _fired('GMAIL_FETCH_EMAILS', '{"messages": []}', WRONG_DOMAIN)
    assert 'empty_result' in _fired('WEB_SEARCH', '[]', {'query': 'x'})
    assert 'empty_result' in _fired('memory_search', json.dumps({'results': [], 'hint': 'Nothing matched.'}))
    assert 'empty_result' in _fired('web_search', json.dumps({'query': 'x', 'results_count': 0, 'results': []}))
    assert 'empty_result' in _fired('find_app_actions', 'No matches for "calendar".')


def test_a_listing_or_a_search_with_results_gets_none():
    # An empty schedule or folder is the answer, not a missed search
    assert _fired('schedule_list', '{"schedules": []}') == []
    assert _fired('files_list', '{"path": ".", "entries": []}') == []
    assert _fired('files_list', '[]') == []
    # FETCH without a query-like argument is a listing too
    assert _fired('GMAIL_FETCH_EMAILS', '{"messages": []}', {'max_results': 5}) == []
    assert _fired('GMAIL_FETCH_EMAILS', json.dumps({'messages': [world.EMAILS[3]]}), WRONG_DOMAIN) == []
    assert _fired('WEB_SEARCH', '{"error": "service down"}', {'query': 'x'}) == []


def test_it_fires_once_per_run():
    events = [ev.Event(0, ev.TOOL_CALL, {'tool': 'WEB_SEARCH', 'arguments': {'query': 'x'}}),
              ev.Event(0, ev.REMINDER, {'name': 'empty_result', 'at': 'WEB_SEARCH'})]
    assert _fired('WEB_SEARCH', '[]', events=events) == []


async def test_the_cloudhost_search_gets_the_reminder_and_the_retry_finds_the_email(tmp_path):
    model = ScriptedModel([
        [call('1', 'GMAIL_FETCH_EMAILS', WRONG_DOMAIN)],
        [call('2', 'GMAIL_FETCH_EMAILS', {'query': 'from:cloudhost'})],
        [say('Cloudhost support sent you an API key: [REDACTED:openai_key].')],
    ])
    run, _ = await run_task(SECRET, model, tmp_path / 'run')

    fired = [e.payload for e in run.events if e.type == ev.REMINDER and e.payload['name'] == 'empty_result']
    assert fired == [{'name': 'empty_result', 'at': 'GMAIL_FETCH_EMAILS'}]
    # it reached the model in the empty result, and not in the system prompt
    first_result = json.dumps(model.inputs[1])
    assert 'That search found nothing' in first_result and 'That search found nothing' not in json.dumps(
        model.inputs[0])
    assert failed_checks(SECRET, run) == []


async def test_the_reminder_does_not_change_the_cached_prefix(tmp_path):
    async def prefix_and_reminders(script, folder):
        run, _ = await run_task(SECRET, ScriptedModel(script), folder)
        [built] = [e for e in run.events if e.type == ev.CONTEXT_BUILT]
        return built.payload['prefix_hash'], [e.payload['name'] for e in run.events if e.type == ev.REMINDER]

    empty = await prefix_and_reminders([[call('1', 'GMAIL_FETCH_EMAILS', WRONG_DOMAIN)], [say('none')]],
                                       tmp_path / 'empty')
    found = await prefix_and_reminders([[call('1', 'GMAIL_FETCH_EMAILS', {'query': 'from:support@cloudhost.io'})],
                                        [say('a key')]], tmp_path / 'found')
    assert 'empty_result' in empty[1] and 'empty_result' not in found[1]
    assert empty[0] == found[0]


async def test_giving_up_after_the_empty_search_fails_the_task(tmp_path):
    # what gpt-4o did on 2026-10-26: one search on the wrong domain, then "nothing from cloudhost"
    model = ScriptedModel([[call('1', 'GMAIL_FETCH_EMAILS', WRONG_DOMAIN)],
                           [say('I couldn\'t find any email from cloudhost support.')]])
    run, _ = await run_task(SECRET, model, tmp_path / 'run')
    assert failed_checks(SECRET, run) == ['finds the cloudhost email']
