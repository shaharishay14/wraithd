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


# --- weighted tokens -------------------------------------------------------------------------------------------

def claude_call(input_tokens, output_tokens, cache_read, cache_write):
    """A claude-code model_output: input_tokens already includes cache reads and writes (usage_numbers)."""
    return (ev.MODEL_OUTPUT, {'engine': 'claude-code', 'input_tokens': input_tokens, 'output_tokens': output_tokens,
                              'cache_read': cache_read, 'cache_write': cache_write})


def test_cache_reads_weigh_a_tenth_and_the_rest_counts_in_full():
    # run 47621a78 (news-feed): input 167,791 of which 155,024 cache reads and 12,751 cache writes, 1,182 out
    events = log_of(claude_call(167_791, 1_182, 155_024, 12_751))
    assert views.tokens_used(events) == 168_973
    assert views.weighted_tokens(events) == (167_791 - 155_024) + 1_182 + 15_502  # 29,451


def test_without_a_cache_split_weighted_is_raw():
    events = log_of((ev.MODEL_OUTPUT, {'input_tokens': 900, 'output_tokens': 100}),
                    (ev.TOOL_RESULT, {'tool': 'web_search', 'usage': {'input_tokens': 40, 'output_tokens': 10}}))
    assert views.weighted_tokens(events) == views.tokens_used(events) == 1050


def test_the_run_budget_counts_weighted_tokens():
    events = log_of(claude_call(167_791, 1_182, 155_024, 12_751))
    assert StopPolicy(token_budget=50_000).check(events) is None  # 169k raw, 29k weighted
    stopped = StopPolicy(token_budget=20_000).check(events)
    assert stopped.code == 'token_budget' and '29451 tokens' in stopped.message


def test_the_daily_budget_says_it_is_wraithds_own_limit():
    reason = StopPolicy(daily_token_budget=2_000_000, tokens_before_run=1_990_000).check(
        log_of(claude_call(167_791, 1_182, 155_024, 12_751)))
    assert reason.code == 'daily_budget'
    assert reason.message == ("wraithd's own daily limit ([budget] daily_tokens = 2,000,000 weighted tokens) is "
                              "reached. This isn't your Claude plan's limit. Raise it in ~/.wraithd/config.toml under "
                              '[budget], or it resets at midnight.')


def test_the_daily_total_sums_weighted_where_a_run_recorded_it(store):
    store.save_events('new', [ev.Event(seq=0, type=ev.RUN_END, payload={'tokens': 168_973, 'weighted_tokens': 29_451})])
    store.save_events('old', [ev.Event(seq=0, type=ev.RUN_END, payload={'tokens': 500})])  # from before 0.1.2
    assert tokens_used_today(store) == 29_951
