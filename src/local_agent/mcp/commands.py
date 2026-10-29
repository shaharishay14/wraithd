"""
`local-agent mcp ...`: add, list, inspect, allow and remove the owner's MCP servers.

    local-agent mcp add gmail --provides email --env GMAIL_TOKEN -- npx -y some-gmail-mcp
    local-agent mcp add notes --url https://notes.example/mcp --header Authorization
    local-agent mcp tools gmail              connect and show each tool's tier
    local-agent mcp allow gmail send_email   run it without asking from now on
    local-agent mcp disallow gmail send_email
    local-agent mcp remove gmail

A secret given as NAME alone is asked for without echo, so it never lands in the shell history. `allow` connects to
check the tool and pins the allow to its exact definition (mcp/pinning.py): a tool the server marks destructive, or
one that's new or changed and not reviewed yet, is refused, and so is any tool while the server doesn't connect.
"""

import asyncio
import getpass
import sys
from dataclasses import replace

from local_agent.config import ConfigError, Home, load_config
from local_agent.mcp import config as mcp_config
from local_agent.mcp import pinning
from local_agent.mcp.client import McpConnections
from local_agent.mcp.config import McpConfigError, McpServer
from local_agent.store import SQLiteStore


def add_arguments(subparsers) -> None:
    mcp = subparsers.add_parser('mcp', help='the MCP servers that connect your apps').add_subparsers(
        dest='mcp_command', required=True)
    add = mcp.add_parser('add', help='add (or replace) a server: a local command after --, or --url',
                         usage='local-agent mcp add NAME [options] [-- COMMAND [ARG ...]]')
    add.add_argument('name', help='a short name, e.g. gmail')
    add.add_argument('--provides', default='', help='capabilities for skills, comma-separated, e.g. email,calendar')
    add.add_argument('--env', action='append', default=[], metavar='KEY[=VALUE]',
                     help='a secret env var for a local server (asked for if no value)')
    add.add_argument('--url', default='', help='a remote server\'s streamable HTTP URL')
    add.add_argument('--header', action='append', default=[], metavar='NAME[: VALUE]',
                     help='a secret header for a remote server (asked for if no value)')
    # The command after -- isn't an argparse positional: cli.main splits it off before parsing (split_command), since
    # Python 3.12.3's argparse (Ubuntu's) rejects a -- that comes after options once NAME has been read.
    # dest isn't "command": that's the top-level subcommand
    add.set_defaults(server_command=[])
    mcp.add_parser('list', help='the configured servers')
    tools = mcp.add_parser('tools', help='connect to a server and show its tools and their tiers')
    tools.add_argument('name')
    for verb, text in (('allow', 'run a tool without asking (it still follows the taint rules)'),
                       ('disallow', 'ask again before a tool runs')):
        parser = mcp.add_parser(verb, help=text)
        parser.add_argument('name')
        parser.add_argument('tool', help='the tool\'s name as the server gives it, e.g. send_email')
    remove = mcp.add_parser('remove', help='remove a server and its secrets')
    remove.add_argument('name')


def split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """`mcp add ... -- COMMAND ARGS`: (the arguments before the first --, the local server's command line after it)."""
    if argv[:2] != ['mcp', 'add'] or '--' not in argv:
        return argv, []
    at = argv.index('--')
    return argv[:at], argv[at + 1:]


def _server(home: Home, name: str) -> McpServer | None:
    return next((server for server in load_config(home).mcp_servers if server.name == name), None)


def _secret_pairs(items, separator: str, kind: str, server: str, ask) -> dict[str, str]:
    pairs = {}
    for item in items:
        key, found, value = item.partition(separator)
        key, value = key.strip(), value.strip()
        if not key:
            raise McpConfigError(f'Give the {kind} as NAME or NAME{separator}VALUE.')
        pairs[key] = value if found and value else ask(f'{kind} {key} for {server} (not shown): ')
        if not pairs[key]:
            raise McpConfigError(f'No value given for {key}.')
    return pairs


def add(home: Home, args, ask=None) -> int:
    ask = ask or getpass.getpass  # looked up now, so it can be replaced
    command = list(args.server_command)
    if bool(args.url) == bool(command):
        print('Give either a command after -- (a local server) or --url (a remote one).', file=sys.stderr)
        return 1
    try:
        env = _secret_pairs(args.env, '=', 'env', args.name, ask)
        headers = _secret_pairs(args.header, ':', 'header', args.name, ask)
        server = mcp_config.validated(McpServer(
            name=args.name, transport='http' if args.url else 'stdio', command=command[0] if command else '',
            args=tuple(command[1:]), env=tuple(env), url=args.url, headers=tuple(headers),
            provides=tuple(p.strip().lower() for p in args.provides.split(',') if p.strip())))
        if args.url and env or command and headers:
            raise McpConfigError('--env is for local servers and --header for remote ones.')
        previous = _server(home, args.name)
    except (McpConfigError, ConfigError) as e:
        print(e, file=sys.stderr)
        return 1
    if previous:
        mcp_config.delete_secrets(previous)
    for key, value in env.items():
        mcp_config.set_secret(server.name, 'env', key, value)
    for key, value in headers.items():
        mcp_config.set_secret(server.name, 'header', key, value)
    mcp_config.write_server(home.config_path, server, server.name)
    print(f'{"Replaced" if previous else "Added"} {server.name}'
          + (f', providing {", ".join(server.provides)}' if server.provides else '')
          + f'. Check its tools with: local-agent mcp tools {server.name}')
    return 0


def list_servers(home: Home) -> int:
    servers = load_config(home).mcp_servers
    if not servers:
        print('No MCP servers. Add one with: local-agent mcp add <name> -- <command>')
        return 0
    for server in servers:
        where = ' '.join([server.command, *server.args]) if server.transport == 'stdio' else server.url
        print(f'{server.name}  ({server.transport}) {where}')
        if server.provides:
            print(f'    provides: {", ".join(server.provides)}')
        if server.env or server.headers:
            print(f'    secrets in the keychain: {", ".join(server.env + server.headers)}')
        if server.always_allow:
            print(f'    always allowed: {", ".join(server.always_allow)}')
    return 0


def _tier(connected, name: str) -> str:
    verdict = connected.pins.get(name)
    if verdict is not None and verdict.state != pinning.OK:
        label = pinning.LABELS[verdict.state]
        return label + (f' (review #{verdict.review_id})' if verdict.review_id else '')
    return ('read-only' if connected.access[name].value == 'read' else
            'destructive (asks every time)' if name in connected.destructive else
            'always allowed' if name in connected.always_allowed else 'asks')


async def _tools(server: McpServer, store) -> int:
    async with McpConnections([server], store=store) as connected:
        if server.name in connected.errors:
            print(f'{server.name} did not connect: {connected.errors[server.name]}', file=sys.stderr)
            return 1
        for tool in connected.tools:
            _, original = connected.origin[tool.name]
            summary = (tool.description or '').strip().splitlines()[0][:70] if (tool.description or '').strip() else ''
            print(f'{original:<28} {_tier(connected, tool.name):<15} {summary}')
    return 0


async def _check(server: McpServer, tool: str, store) -> str | None:
    """Connects (recording what the server shows now) and says why the tool can't be allowed, or None if it can."""
    async with McpConnections([server], store=store) as connected:
        if server.name in connected.errors:
            return (f'{server.name} did not connect ({connected.errors[server.name]}), so {tool} can\'t be checked '
                    'and pinned. Try again once it starts.')
        if connected.marked_destructive(server.name, tool):
            return (f'{server.name} marks {tool} as destructive: it asks every time and never runs unattended, so '
                    'it can\'t be always allowed.')
    return None


def tools(home: Home, name: str) -> int:
    server = _server(home, name)
    if server is None:
        print(f'No MCP server named {name}.', file=sys.stderr)
        return 1
    store = SQLiteStore(home.db_path)
    try:
        return asyncio.run(_tools(server, store))
    finally:
        store.close()


def set_allowed(home: Home, name: str, tool: str, allowed: bool) -> int:
    server = _server(home, name)
    if server is None:
        print(f'No MCP server named {name}.', file=sys.stderr)
        return 1
    store = SQLiteStore(home.db_path)
    try:
        if allowed:
            problem = asyncio.run(_check(server, tool, store))
            if problem is None:
                try:
                    pinning.pin(store, name, tool)
                except pinning.PinError as e:
                    problem = str(e)
            if problem:
                print(problem, file=sys.stderr)
                return 1
        else:
            pinning.unpin(store, name, tool)
    finally:
        store.close()
    current = [t for t in server.always_allow if t != tool] + ([tool] if allowed else [])
    mcp_config.write_server(home.config_path, replace(server, always_allow=tuple(current)), name)
    print(f'{name} {tool}: ' + ('runs without asking while it stays as it is now (read-only tools always do).'
                                if allowed else 'asks first.'))
    return 0


def remove(home: Home, name: str) -> int:
    server = _server(home, name)
    if server is None:
        print(f'No MCP server named {name}.', file=sys.stderr)
        return 1
    mcp_config.delete_secrets(server)
    mcp_config.write_server(home.config_path, None, name)
    store = SQLiteStore(home.db_path)
    try:
        pinning.forget(store, name)
    finally:
        store.close()
    print(f'Removed {name} and its secrets.')
    return 0


def main(home: Home, args) -> int:
    home.ensure()
    try:
        if args.mcp_command == 'add':
            return add(home, args)
        if args.mcp_command == 'list':
            return list_servers(home)
        if args.mcp_command == 'tools':
            return tools(home, args.name)
        if args.mcp_command in ('allow', 'disallow'):
            return set_allowed(home, args.name, args.tool, args.mcp_command == 'allow')
        return remove(home, args.name)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 1
