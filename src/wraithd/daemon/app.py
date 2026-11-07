"""
The daemon: one async process with the local API (and the web UI), the scheduler, Telegram long polling, open MCP
connections and the periodic jobs (daemon/jobs.py: background distill, and any a module adds).
`wraithd daemon` runs it in the foreground; daemon/service.py runs that as a background service.

Everything is opened once in start() and shared: MCP sessions, the Telegram bot, the notifier. Runs from every
channel use the same Runtime, so they share the store, memory and skills.

MCP sessions come in sets, each opened and closed by a task of its own (an MCP client must close in the task that
opened it). When the owner adds a server from Telegram (/mcp_add), reconnect_mcp() opens a new set first and moves
every channel and the scheduler to it; the old set stays open until the runs that started with it have ended (at
most RETIRE_AFTER_SECONDS, longer than any run can take), so a run in progress keeps its tools.
"""

import asyncio
import os
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from wraithd.channels.telegram import TelegramBot, TelegramChannel, TelegramNotifier
from wraithd.config import get_key
from wraithd.daemon import jobs, service
from wraithd.daemon.api import build_api
from wraithd.daemon.jobs import DISTILL_EVERY_SECONDS, distill_all  # noqa: F401 (where they used to live)
from wraithd.daemon.scheduler import Scheduler
from wraithd.daemon.security import Guard, load_or_create_token, remote_hosts
from wraithd.mcp.client import McpConnections
from wraithd.run import RETRY_DELAYS, RUN_TIMEOUT_SECONDS

# How long an old MCP session set may wait for its runs: a run's attempts, their pauses, and a minute to spare
RETIRE_AFTER_SECONDS = RUN_TIMEOUT_SECONDS * (len(RETRY_DELAYS) + 1) + sum(RETRY_DELAYS) + 60

HOST = '127.0.0.1'  # never 0.0.0.0: nothing else on the network may reach the agent
UI_ENV = 'WRAITHD_UI_DIR'
PACKAGE_UI = Path(__file__).resolve().parent.parent / 'ui'  # where scripts/release.sh puts the build


def ui_dir() -> Path | None:
    """The built web UI: $WRAITHD_UI_DIR, else the ui/ folder in the package, if it has an index.html."""
    for candidate in (os.environ.get(UI_ENV), PACKAGE_UI):
        if candidate and (Path(candidate) / 'index.html').is_file():
            return Path(candidate).resolve()
    return None


class _Sessions:
    """One set of MCP sessions, held open by its own task until released."""

    def __init__(self, servers, store):
        self.ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self.release = asyncio.Event()
        self.task = asyncio.create_task(self._hold(servers, store))

    async def _hold(self, servers, store) -> None:
        try:
            async with McpConnections(servers, store=store) as connected:
                self.ready.set_result(connected)
                await self.release.wait()
        except BaseException as e:  # nobody waits forever on sessions that won't open
            if not self.ready.done():
                self.ready.set_exception(e if isinstance(e, Exception) else RuntimeError('MCP sessions stopped'))
            raise

    async def close(self) -> None:
        self.release.set()
        await asyncio.gather(self.task, return_exceptions=True)


class _Scheduler(Scheduler):
    """The scheduler, keeping the tasks of the schedules running now (each may hold an MCP session set)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.firing: set[asyncio.Task] = set()

    async def _fire(self, schedule):
        task = asyncio.current_task()
        self.firing.add(task)
        try:
            return await super()._fire(schedule)
        finally:
            self.firing.discard(task)


class Daemon:
    def __init__(self, rt, telegram_transport=None, clock=lambda: datetime.now(UTC)):
        self.rt, self.clock = rt, clock
        self.telegram_transport = telegram_transport
        self.token = load_or_create_token(rt.home.root)
        self.port = rt.config.daemon_port
        self.mcp = None
        self.telegram: TelegramChannel | None = None
        self.scheduler = _Scheduler(rt, clock=clock)
        self.background: set[asyncio.Task] = set()  # web chat runs that outlive their request
        self._stack = AsyncExitStack()
        self._sessions: _Sessions | None = None  # the MCP set new runs get
        self._retiring: set[asyncio.Task] = set()  # old sets waiting for their runs to end
        self._reconnecting = asyncio.Lock()

    @property
    def url(self) -> str:
        return f'http://{HOST}:{self.port}/#token={self.token}'

    @property
    def remote_urls(self) -> list[str]:
        """The same UI through tailscale serve, one per [remote] allowed_hosts name (docs/remote-access.md)."""
        return [f'https://{host.removesuffix(":443")}/#token={self.token}' for host in remote_hosts(self.rt.config)]

    async def start(self) -> None:
        service.rotate_log(self.rt.home)  # a log that grew while the daemon was down
        await self._stack.__aenter__()
        # Owner-only, long-lived: every owner run in the daemon shares these sessions
        self._sessions = _Sessions(self.rt.config.mcp_servers, self.rt.store)
        self.mcp = self.scheduler.mcp = await asyncio.shield(self._sessions.ready)
        token = get_key('telegram')
        if token:
            bot = TelegramBot(token, self.telegram_transport)
            self._stack.push_async_callback(bot.close)
            self.telegram = TelegramChannel(self.rt, bot, mcp=self.mcp, clock=self.clock)
            self.telegram.reconnect_mcp = self.reconnect_mcp
            self.rt.notifier = TelegramNotifier(self.rt.store, bot)

    def _runs(self) -> set[asyncio.Task]:
        """The tasks that may be running with the current MCP set: web chat runs, Telegram messages, schedules."""
        return {*self.background, *(self.telegram.tasks if self.telegram else ()), *self.scheduler.firing}

    async def reconnect_mcp(self):
        """Opens a new MCP set from rt.config (after a server was added), moves the channels and the scheduler to
        it, and returns it. The old set closes once the runs that may hold it have ended."""
        async with self._reconnecting:
            if self._sessions is None:
                return self.mcp
            new = _Sessions(self.rt.config.mcp_servers, self.rt.store)
            try:
                connected = await asyncio.shield(new.ready)
            except Exception:
                await new.close()
                raise
            old, self._sessions = self._sessions, new
            self.mcp = self.scheduler.mcp = connected
            if self.telegram:
                self.telegram.mcp = connected
            holders = self._runs() - {asyncio.current_task()}  # the run asking for this holds nothing of the old set
            task = asyncio.create_task(self._retire(old, holders))
            self._retiring.add(task)
            task.add_done_callback(self._retiring.discard)
            return connected

    async def _retire(self, old: _Sessions, holders: set[asyncio.Task]) -> None:
        try:
            if holders:
                await asyncio.wait(holders, timeout=RETIRE_AFTER_SECONDS)
        finally:  # stop() cancels the wait: the set still closes
            await old.close()

    async def stop(self) -> None:
        if self.telegram:
            await self.telegram.settle()
        if self.background:
            await asyncio.gather(*list(self.background), return_exceptions=True)
        for task in list(self._retiring):
            task.cancel()
        await asyncio.gather(*self._retiring, return_exceptions=True)
        if self._sessions is not None:
            await self._sessions.close()
        await self._stack.__aexit__(None, None, None)

    def asgi(self):
        """The API and UI behind the Host, Origin and token checks."""
        return Guard(build_api(self, ui_dir()), self.token, self.port, self.rt.config.dev_origin,
                     remote_hosts(self.rt.config))

    def uvicorn_config(self) -> uvicorn.Config:
        return uvicorn.Config(self.asgi(), host=HOST, port=self.port, lifespan='off', log_level='warning',
                              server_header=False, date_header=False)

    async def serve(self, announce=lambda text: print(text, flush=True)) -> None:
        await self.start()
        try:
            server = uvicorn.Server(self.uvicorn_config())
            tasks = [asyncio.create_task(server.serve()), asyncio.create_task(self.scheduler.run_forever()),
                     *(asyncio.create_task(jobs.run_forever(self.rt, job)) for job in jobs.jobs(self.rt))]
            if self.telegram:
                tasks.append(asyncio.create_task(self.telegram.run_forever()))
            announce(f'wraithd is running. Open {self.url}')
            for url in self.remote_urls:
                announce(f'From your tailnet (through tailscale serve): {url}')
            for name, problem in (self.mcp.errors if self.mcp else {}).items():
                announce(f'(MCP server {name} is not available: {problem})')
            announce('Telegram: ' + ('on' if self.telegram else 'off (wraithd key set telegram)'))
            # The server ends on Ctrl-C; then stop everything else with it
            await tasks[0]
            for task in tasks[1:]:
                task.cancel()
            await asyncio.gather(*tasks[1:], return_exceptions=True)
        finally:
            await self.stop()
