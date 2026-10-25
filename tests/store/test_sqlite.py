"""
The SQLite store: what the harness needs from it (events in order, the previous run, today's tokens, the
approval trail), and that it survives reopening the file.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from local_agent.core import events as ev
from local_agent.store import SQLiteStore, Store
from local_agent.store import base as store_base

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def event(seq, event_type, created_at=T0, **payload):
    return ev.Event(seq=seq, type=event_type, payload=payload, created_at=created_at)


def test_it_implements_the_store_interface(store):
    store_methods = {name for name in dir(Store) if not name.startswith('_')}
    assert store_methods <= set(dir(store))


def test_events_come_back_in_order_with_their_payloads(store):
    store.save_events('run-1', [event(1, ev.TURN_START, turn=1), event(0, ev.RUN_START, source='chat', message='hi')])
    store.save_events('run-2', [event(0, ev.RUN_START, source='chat')])
    events = store.run_events('run-1')
    assert [(e.seq, e.type) for e in events] == [(0, ev.RUN_START), (1, ev.TURN_START)]
    assert events[0].payload == {'source': 'chat', 'message': 'hi'} and events[0].created_at == T0
    assert store.run_events('missing') == []


def test_the_latest_run_is_by_source_and_time(store):
    store.save_events('old', [event(0, ev.RUN_START, T0 - timedelta(hours=1), source='chat')])
    store.save_events('new', [event(0, ev.RUN_START, T0, source='chat')])
    store.save_events('cron', [event(0, ev.RUN_START, T0 + timedelta(minutes=5), source='schedule')])
    assert store.latest_run(source='chat', since=T0 - timedelta(hours=2)) == 'new'
    assert store.latest_run(source='chat', since=T0 - timedelta(hours=2), exclude='new') == 'old'
    assert store.latest_run(source='chat', since=T0 + timedelta(minutes=1)) is None
    assert store.latest_run(source='schedule', since=T0) == 'cron'


def test_times_compare_correctly_across_timezones(store):
    # 14:00 at UTC+3 is 11:00 UTC, before T0 (12:00 UTC), though "14:00" sorts after "12:00" as text
    plus_three = timezone(timedelta(hours=3))
    store.save_events('a', [event(0, ev.RUN_END, datetime(2026, 5, 1, 14, 0, tzinfo=plus_three), tokens=5)])
    store.save_events('b', [event(0, ev.RUN_END, T0, tokens=7)])
    assert store.tokens_since(T0) == 7
    assert store.tokens_since(datetime(2026, 5, 1, 10, 30, tzinfo=UTC)) == 12


def test_tokens_since_adds_up_run_ends_and_ignores_missing_counts(store):
    store.save_events('a', [event(0, ev.RUN_END, T0, tokens=100), event(1, ev.MODEL_OUTPUT, T0, input_tokens=999)])
    store.save_events('b', [event(0, ev.RUN_END, T0 + timedelta(seconds=1), outcome='refused')])
    assert store.tokens_since(T0) == 100
    assert store.tokens_since(T0 + timedelta(hours=1)) == 0


def test_events_between_runs_have_no_run(store):
    ev.record_event(store, ev.COMPACTION, messages_folded=12)
    assert store._query('SELECT run_id, type FROM run_events')[0]['run_id'] is None


def test_an_approval_is_decided_once(store):
    approval = store.create_approval('GMAIL_SEND_EMAIL', {'recipient_email': 'dana@co.com'})
    assert store.get_approval(approval.id).status == store_base.PENDING and approval.decided_at is None
    assert store.decide_approval(approval.id, store_base.APPROVED) is True
    assert store.decide_approval(approval.id, store_base.EXPIRED) is False  # too late
    decided = store.get_approval(approval.id)
    assert decided.status == store_base.APPROVED and decided.decided_at is not None
    assert decided.arguments == {'recipient_email': 'dana@co.com'}
    assert store.get_approval('missing') is None


def test_bad_statuses_are_refused(store):
    approval = store.create_approval('x', {})
    with pytest.raises(ValueError):
        store.decide_approval(approval.id, store_base.PENDING)
    with pytest.raises(ValueError):
        store.create_approval('x', {}, status='maybe')


def test_the_file_survives_reopening(tmp_path):
    path = tmp_path / 'state' / 'state.db'
    first = SQLiteStore(path)
    first.save_events('run-1', [event(0, ev.RUN_START, source='chat')])
    approval = first.create_approval('x', {})
    first.close()

    second = SQLiteStore(path)
    assert [e.type for e in second.run_events('run-1')] == [ev.RUN_START]
    assert second.get_approval(approval.id).status == store_base.PENDING
    second.close()


def test_a_failed_save_never_breaks_the_run(store, caplog):
    store.close()  # every write now fails
    log = ev.EventLog(store)
    log.append(ev.RUN_START, source='chat')
    log.save()
    assert 'Could not save 1 run events' in caplog.text


def test_conversations_keep_their_messages_and_cursors(store):
    conversation = store.start_conversation('terminal')
    first = store.add_message(conversation.id, 'user', 'hi')
    second = store.add_message(conversation.id, 'assistant', 'hello')
    store.add_message(store.start_conversation('terminal').id, 'user', 'other conversation')
    assert [m.content for m in store.messages(conversation.id)] == ['hi', 'hello']
    assert [m.content for m in store.messages(conversation.id, after=first.id)] == ['hello']

    store.update_conversation(conversation.id, summary='S', summarized_until=first.id, distilled_until=second.id)
    stored = store.get_conversation(conversation.id)
    assert (stored.summary, stored.summarized_until, stored.distilled_until) == ('S', first.id, second.id)
    with pytest.raises(ValueError):
        store.update_conversation(conversation.id, source='hacked')
    assert store.get_conversation('missing') is None


def test_recent_runs_and_short_ids(store):
    store.save_events('abc-1', [event(0, ev.RUN_START, T0, source='chat', message='first')])
    store.save_events('abd-2', [event(0, ev.RUN_START, T0 + timedelta(seconds=1), source='schedule', message='x')])
    store.save_events('a_c-3', [event(0, ev.RUN_START, T0 - timedelta(seconds=1), source='chat', message='y')])
    assert [(r.run_id, r.source, r.message) for r in store.recent_runs(limit=2)] == [('abd-2', 'schedule', 'x'),
                                                                                    ('abc-1', 'chat', 'first')]
    assert store.find_runs('ab') == ['abc-1', 'abd-2'] and store.find_runs('abc') == ['abc-1']
    assert store.find_runs('a_') == ['a_c-3']  # "_" is matched as written, not as a wildcard
    assert store.find_runs('%') == []


def test_an_older_database_gets_the_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / 'old.db'
    old = sqlite3.connect(path)
    old.executescript('CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, '
                      'role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);'
                      "INSERT INTO messages (conversation_id, role, content, created_at) "
                      "VALUES ('c1', 'user', 'from before', '2026-01-01T00:00:00.000000+00:00');")
    old.commit()
    old.close()
    store = SQLiteStore(path)
    [message] = store.messages('c1')
    assert (message.content, message.run_id) == ('from before', None)
    assert store.add_message('c1', 'assistant', 'now', run_id='r1').run_id == 'r1'
    store.close()
