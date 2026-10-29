"""Periodic jobs: background distill, and any job a module registers."""

import asyncio
import logging

import pytest

from local_agent.daemon import jobs
from local_agent.daemon.jobs import Job
from tests.scripted import ScriptedModel


def test_distill_is_a_periodic_job(make_runtime):
    distill = next(job for job in jobs.jobs(make_runtime(ScriptedModel([]))) if job.name == 'distill')
    assert (distill.name, distill.every_seconds, distill.run) == ('distill', 3600, jobs.distill_all)


async def test_a_job_runs_every_interval_and_survives_a_failure(caplog):
    ran, waits = [], []

    async def flaky(rt):
        ran.append(rt)
        if len(ran) == 1:
            raise RuntimeError('model down')
        if len(ran) == 3:
            raise asyncio.CancelledError  # the daemon stopping

    async def sleep(seconds):
        waits.append(seconds)

    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await jobs.run_forever('rt', Job('flaky', 60, flaky), sleep=sleep)
    assert ran == ['rt'] * 3 and waits == [60] * 3
    assert 'Periodic job flaky failed: RuntimeError: model down' in caplog.text


async def test_the_daemon_starts_every_registered_job(monkeypatch, make_runtime):
    from local_agent.daemon import app

    started = asyncio.Event()

    async def job(rt):
        started.set()

    monkeypatch.setattr(jobs, 'jobs', lambda rt: [Job('quick', 0, job)])
    daemon = app.Daemon(make_runtime(ScriptedModel([])))
    daemon.port = 0

    class Server:
        def __init__(self, config):
            pass

        async def serve(self):
            await asyncio.wait_for(started.wait(), timeout=5)

    monkeypatch.setattr(app.uvicorn, 'Server', Server)
    await daemon.serve(announce=lambda text: None)
    assert started.is_set()
