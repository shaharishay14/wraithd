"""
The daemon: one async process with the local API (and the web UI), the scheduler, Telegram long polling, open MCP
connections and the periodic jobs (daemon/jobs.py: background distill, and any a module adds).
`wraithd daemon` runs it in the foreground; daemon/service.py runs that as a background service.

Everything is opened once in start() and shared: MCP sessions, the Telegram bot, the notifier. Runs from every
channel use the same Runtime, so they share the store, memory and skills. The MCP sessions live in one task of their
own (their clients must close in the task that opened them), which reopens them all when the owner adds a server from
Telegram (/mcp_add): reconnect_mcp().
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

HOST = '127.0.0.1'  # never 0.0.0.0: nothing else on the network may reach the agent
UI_ENV = 'WRAITHD_UI_DIR'
PACKAGE_UI = Path(__file__).resolve().parent.parent / 'ui'  # where scripts/release.sh puts the build


def ui_dir() -> Path | None:
    """The built web UI: $WRAITHD_UI_DIR, else the ui/ folder in the package, if it has an index.html."""
    for candidate in (os.environ.get(UI_ENV), PACKAGE_UI):
        if candidate and (Path(candidate) / 'index.html').is_file():
            return Path(candidate).resolve()
    return None


class Daemon:
    def __init__(self, rt, telegram_transport=None, clock=lambda: datetime.now(UTC)):
        self.rt, self.clock = rt, clock
        self.telegram_transport = telegram_transport
        self.token = load_or_create_token(rt.home.root)
        self.port = rt.config.daemon_port
        self.mcp = None
        self.telegram: TelegramChannel | None = None
        self.scheduler = Scheduler(rt, clock=clock)
        self.background: set[asyncio.Task] = set()  # web chat runs that outlive their request
        self._stack = AsyncExitStack()
        self._mcp_task: asyncio.Task | None = None
        self._mcp_wanted = asyncio.Event()  # set: close the MCP sessions (and reopen them unless stopping)
        self._mcp_ready: asyncio.Future | None = None
        self._mcp_stopping = False

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
        self._mcp_ready = asyncio.get_running_loop().create_future()
        self._mcp_task = asyncio.create_task(self._hold_mcp())
        await asyncio.shield(self._mcp_ready)
        token = get_key('telegram')
        if token:
            bot = TelegramBot(token, self.telegram_transport)
            self._stack.push_async_callback(bot.close)
            self.telegram = TelegramChannel(self.rt, bot, mcp=self.mcp, clock=self.clock)
            self.telegram.reconnect_mcp = self.reconnect_mcp
            self.rt.notifier = TelegramNotifier(self.rt.store, bot)

    async def _hold_mcp(self) -> None:
        """Opens the MCP sessions, keeps them until asked to reopen (or stop), and closes them, in this one task."""
        try:
            while True:
                self._mcp_wanted.clear()
                async with McpConnections(self.rt.config.mcp_servers, store=self.rt.store) as connected:
                    self.mcp = self.scheduler.mcp = connected
                    if self.telegram:
                        self.telegram.mcp = connected
                    if not self._mcp_ready.done():
                        self._mcp_ready.set_result(connected)
                    await self._mcp_wanted.wait()
                if self._mcp_stopping:
                    return
        except BaseException as e:  # nobody waits forever on sessions that won't open
            if not self._mcp_ready.done():
                self._mcp_ready.set_exception(e if isinstance(e, Exception) else RuntimeError('MCP task stopped'))
            raise

    async def reconnect_mcp(self):
        """Closes the MCP sessions and opens them again from rt.config (after a server was added); returns the new
        ones. A run that is using the old sessions at that moment loses them."""
        if self._mcp_task is None or self._mcp_task.done():
            return self.mcp
        self._mcp_ready = asyncio.get_running_loop().create_future()
        self._mcp_wanted.set()
        return await asyncio.shield(self._mcp_ready)

    async def stop(self) -> None:
        if self.telegram:
            await self.telegram.settle()
        if self.background:
            await asyncio.gather(*list(self.background), return_exceptions=True)
        if self._mcp_task is not None:
            self._mcp_stopping = True
            self._mcp_wanted.set()
            await asyncio.gather(self._mcp_task, return_exceptions=True)
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
