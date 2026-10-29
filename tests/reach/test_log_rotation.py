"""
The daemon log rotates at a size limit: copied to daemon.log.1 (older ones shift, the oldest goes) and emptied in
place, since the service manager keeps appending to the same open file. `daemon logs` reads across the rotation.
"""

import argparse
import os
import stat

import pytest

from local_agent.daemon import commands, jobs, service
from local_agent.daemon.app import Daemon
from tests.scripted import ScriptedModel


@pytest.fixture
def log(home):
    path = service.log_path(home)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    return path


def logs(home, lines=50):
    said = []
    assert commands.main(home, argparse.Namespace(daemon_command='logs', lines=lines, follow=False),
                         say=said.append) == 0
    return said


def test_the_log_rotates_past_the_limit_and_stays_the_same_file(home, log):
    log.write_text('a\n' * 10)
    assert service.rotate_log(home, max_bytes=100) is False  # under the limit: untouched
    with open(log, 'a') as writer:  # what launchd and systemd hold: an append-mode handle
        writer.write('b\n' * 60)
        writer.flush()
        inode = os.stat(log).st_ino
        assert service.rotate_log(home, max_bytes=100) is True
        [first, second, third] = service.rotated_log_paths(home)
        assert first.read_text() == 'a\n' * 10 + 'b\n' * 60 and not second.exists()
        assert log.read_text() == '' and os.stat(log).st_ino == inode
        assert stat.S_IMODE(first.stat().st_mode) == 0o600
        writer.write('after\n')  # still lands in daemon.log, at its start
    assert log.read_text() == 'after\n'


def test_old_logs_shift_and_the_oldest_is_dropped(home, log):
    for round_ in range(5):
        log.write_text(f'round {round_}\n' * 20)
        assert service.rotate_log(home, max_bytes=50)
    assert [p.read_text().splitlines()[0] for p in service.rotated_log_paths(home)] == ['round 4', 'round 3',
                                                                                         'round 2']
    assert not log.with_name('daemon.log.4').exists()


def test_no_log_no_rotation(home):
    assert service.rotate_log(home) is False


async def test_the_daemon_rotates_at_start_and_every_minute(make_runtime, home, log, monkeypatch):
    rt = make_runtime(ScriptedModel([]))
    [job] = [job for job in jobs.jobs(rt) if job.name == 'log-rotate']
    assert job.every_seconds == 60
    monkeypatch.setattr(service, 'LOG_MAX_BYTES', 10)
    log.write_text('x' * 20)
    await job.run(rt)
    assert log.read_text() == '' and service.rotated_log_paths(home)[0].read_text() == 'x' * 20

    log.write_text('y' * 20)
    daemon = Daemon(rt)
    await daemon.start()
    await daemon.stop()
    assert log.read_text() == '' and service.rotated_log_paths(home)[0].read_text() == 'y' * 20


def test_daemon_logs_reads_across_the_rotation(home, log):
    log.write_text('one\ntwo\nthree\n')
    service.rotate_log(home, max_bytes=1)
    log.write_text('four\n')
    assert logs(home, lines=3) == ['two', 'three', 'four']
    assert logs(home, lines=1) == ['four']
    assert logs(home, lines=50) == ['one', 'two', 'three', 'four']


def test_daemon_logs_follow_keeps_going_after_a_rotation(home, log):
    log.write_text('old\n')
    rounds = iter(['before', 'rotate', 'after', 'stop'])

    def sleep(_):
        step = next(rounds)
        if step == 'before':
            with open(log, 'a') as out:
                out.write('new line\n')
        elif step == 'rotate':
            with open(log, 'a') as out:
                out.write('x' * 100 + '\n')
            service.rotate_log(home, max_bytes=10)
        elif step == 'after':
            with open(log, 'a') as out:
                out.write('fresh\n')
        else:
            raise KeyboardInterrupt

    said = []
    with pytest.raises(KeyboardInterrupt):
        commands.logs(home, 5, True, said.append, sleep)
    assert said == ['old', 'new line', 'fresh']  # the line rotated away mid-follow is in daemon.log.1
