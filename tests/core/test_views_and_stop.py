"""
The deterministic part of the harness: state derived from the event log, and the stop conditions
read from it. No model, no store.
"""

from datetime import datetime

from wraithd.core import events as ev
from wraithd.core import views
from wraithd.core.events import EventLog
from wraithd.core.stop import StopPolicy, tokens_used_today


def log_of(*steps):
    """An event log built from (type, payload) pairs."""
    log = EventLog()
    for event_type, payload in steps:
        log.append(event_type, **payload)
    return log.events


def call(tool, **arguments):
    return (ev.TOOL_CALL, {'tool': tool, 'arguments': arguments})


def result(tool, outcome='ok', untrusted=False):
    return (ev.TOOL_RESULT, {'tool': tool, 'outcome': outcome, 'untrusted': untrusted})


def test_consecutive_errors_count_back_from_the_latest_and_reset_on_success():
    events = log_of(result('a', 'error'), result('b'), result('c', 'error'), result('d', 'error'))
    assert views.consecutive_tool_errors(events) == 2
    assert views.consecutive_tool_errors(log_of(result('a', 'error'), result('b'))) == 0


def test_a_denial_is_not_an_error():
    # the policy said no: that's the model's cue to explain, not a broken tool
    assert views.consecutive_tool_errors(log_of(result('a', 'error'), result('b', 'denied'))) == 0


def test_repeated_calls_match_on_tool_and_exact_arguments():
    events = log_of(call('search', q='x', n=1), call('search', n=1, q='x'), call('search', q='y'))
    assert views.most_repeated_call(events) == ('search', 2)   # argument order doesn't matter
    assert views.most_repeated_call([]) == (None, 0)


def test_tokens_and_turns_add_up_over_model_calls():
    events = log_of((ev.TURN_START, {}), (ev.MODEL_OUTPUT, {'input_tokens': 100, 'output_tokens': 20}),
                    (ev.TURN_START, {}), (ev.MODEL_OUTPUT, {'input_tokens': 150, 'output_tokens': 5}))
    assert views.turns(events) == 2 and views.tokens_used(events) == 275


def test_untrusted_sources_are_listed_once_in_reading_order():
    events = log_of(result('GMAIL_FETCH', untrusted=True), result('calculate'), result('web_search', untrusted=True),
                    result('GMAIL_FETCH', untrusted=True))
    assert views.untrusted_content_seen(events) == ['GMAIL_FETCH', 'web_search']


def test_stop_policy_names_the_first_condition_that_fires():
    policy = StopPolicy(max_consecutive_errors=3, max_identical_calls=3, token_budget=1000)
    assert policy.check(log_of(result('a'))) is None
    assert policy.check(log_of(*[result('a', 'error')] * 3)).code == 'tool_errors'
    assert policy.check(log_of(*[call('a', x=1)] * 3)).code == 'repeated_call'
    assert policy.check(log_of((ev.MODEL_OUTPUT, {'input_tokens': 990, 'output_tokens': 10}))).code == 'token_budget'
    assert StopPolicy(token_budget=None).check(log_of((ev.MODEL_OUTPUT, {'input_tokens': 10 ** 9}))) is None


def test_long_payload_strings_are_cut_before_storage():
    log = EventLog()
    event = log.append(ev.TOOL_RESULT, preview='x' * 10_000, nested={'list': ['y' * 10_000]})
    assert len(event.payload['preview']) < ev.MAX_PAYLOAD_TEXT + 50
    assert len(event.payload['nested']['list'][0]) < ev.MAX_PAYLOAD_TEXT + 50


def test_skills_loaded_counts_only_loads_the_gate_allowed():
    events = log_of((ev.TOOL_CALL, {'tool': 'skill_load', 'arguments': {'name': 'a'}, 'allowed': True}),
                    (ev.TOOL_CALL, {'tool': 'skill_load', 'arguments': {'name': 'b'}, 'allowed': False}),
                    (ev.TOOL_CALL, {'tool': 'calculate', 'arguments': {'name': 'c'}, 'allowed': True}))
    assert views.skills_loaded(events) == ['a']


def test_the_daily_budget_counts_from_local_midnight(store):
    now = datetime(2026, 3, 2, 9, 0).astimezone()
    yesterday, this_morning = now.replace(day=1, hour=23), now.replace(hour=1)
    for when, tokens in [(yesterday, 500), (this_morning, 300), (now, 200)]:
        store.save_events('run', [ev.Event(seq=0, type=ev.RUN_END, payload={'tokens': tokens}, created_at=when)])
    assert tokens_used_today(store, now) == 500
    policy = StopPolicy(daily_token_budget=1000, tokens_before_run=tokens_used_today(store, now))
    assert not policy.over_daily_budget() and policy.over_daily_budget(used_this_run=500)
    assert policy.check(log_of((ev.MODEL_OUTPUT, {'input_tokens': 500}))).code == 'daily_budget'


def missing(tool, what):
    return (ev.TOOL_RESULT, {'tool': tool, 'outcome': 'error',
                             'preview': '{"error": "' + what + " 'x.md'. Next step: Call files_list.\"}"})


def test_a_missing_file_or_folder_is_not_a_failed_tool_call():
    from wraithd.core.stop import failed_in_a_row

    gone = [missing('files_read', 'There is no file'), missing('files_list', 'There is no folder')]
    assert failed_in_a_row(log_of(*gone, *gone)) == 0
    assert StopPolicy(max_consecutive_errors=3).check(log_of(*gone, *gone)) is None
    # other failures around them still count, and a missing file doesn't end their streak
    assert failed_in_a_row(log_of(result('a', 'error'), *gone, result('b', 'error'), result('c', 'error'))) == 3
    # only files_read / files_list say so: another tool with the same words is a failure
    assert failed_in_a_row(log_of(missing('memory_read', 'There is no file'))) == 1
    # and only at the start of the error, not quoted inside another one
    assert failed_in_a_row(log_of(missing('files_read', 'Bad path: There is no file'))) == 1
