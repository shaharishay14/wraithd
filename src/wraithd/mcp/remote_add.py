"""
`/mcp_add` from the owner's phone: adds an MCP server the way `wraithd mcp add` does, behind a confirm card.

    /mcp_add notes --provides notes -- npx -y some-notes-mcp
    /mcp_add gmail --provides email --env GMAIL_TOKEN -- npx -y some-gmail-mcp
    /mcp_add docs --url https://docs.example/mcp --header Authorization=op://wraithd/Docs/header

What keeps it safe:

- It's a command the channel handles in code (channels/base.py), before any model run, for the paired owner's chat
  only, and never for a forwarded message. No agent tool adds a server, so text the agent reads can't add one.
- The arguments are `wraithd mcp add`'s, parsed by its own parser (mcp/commands.py parse_add).
- No secret goes through the chat (Telegram keeps every message): an --env or --header is a name alone, read from
  WRAITHD_MCP_<SERVER>_<KEY> (the server's env file, set over SSH), or a 1Password op:// reference. A value, or
  anything else in the text that looks like a key or token, refuses the whole command and nothing of it is kept.
- The owner sees the exact command or URL on a card and taps Allow. Only then is the block written; the server is
  connected once, and that first connect pins its tools as they are (mcp/pinning.py).
- An existing server is never replaced from the phone.
"""

import re
import shlex
from dataclasses import replace
from urllib.parse import parse_qsl, urlsplit

from wraithd.config import load_config
from wraithd.core import events as ev
from wraithd.core import secrets
from wraithd.mcp import commands, pinning
from wraithd.mcp import config as mcp_config
from wraithd.mcp.client import McpConnections
from wraithd.mcp.config import McpConfigError, McpServer

COMMAND = '/mcp_add'
MCP_ADDED = 'mcp_added'  # the owner added a server from the phone: its name, transport and tool count, no values
APPROVAL_TOOL = 'mcp_add'  # what the confirm card is stored as, in the approvals table
USAGE = ('Usage: /mcp_add NAME [--provides a,b] [--tools t1,t2] [--sandbox] [--env KEY] -- COMMAND ARGS\n'
         '   or: /mcp_add NAME --url https://... [--header NAME]')
WHERE_SECRETS_GO = ('Secrets never go in this chat (Telegram keeps every message). Give only the name (--env KEY), '
                    'then put the value in the server\'s env file over SSH as {var}, or give a 1Password reference '
                    '(--env KEY=op://vault/item/field).')
RUNNERS = {'npx': (), 'uvx': (), 'bunx': (), 'pipx': ('run',), 'pnpm': ('dlx',), 'uv': ('tool', 'run')}
# --api-key VALUE, --token=VALUE, ... in the server's command line
SECRET_FLAG = re.compile(r'^--?[\w-]*(key|token|secret|passw(or)?d|auth|credential|bearer)[\w-]*$', re.IGNORECASE)
SECRET_QUERY = re.compile(r'key|token|secret|passw(or)?d|auth|sig|credential', re.IGNORECASE)
LONG_OPAQUE = re.compile(r'^[A-Za-z0-9_-]{32,}$')  # a long run of letters and digits, the shape of most tokens
REFERENCE_ANYWHERE = re.compile(r'op://\S+')
PATHLIKE = ('/', './', '../', '~', '$')  # a flag's value that is a file or a variable, not a secret
DASHES = {'—': '--', '–': '--'}  # a phone keyboard turns -- into a dash
QUOTES = str.maketrans({'“': '"', '”': '"', '‘': "'", '’': "'"})


class Refused(Exception):
    """The command can't be used. The message is the reply; secret is True when it looked like it had one."""

    def __init__(self, message: str, secret: bool = False):
        super().__init__(message)
        self.secret = secret


def is_command(text: str) -> bool:
    first = text.split(maxsplit=1)[0] if text.strip() else ''
    return first == COMMAND or first.startswith(COMMAND + '@')  # /mcp_add@SomeBot in a group


def _words(text: str) -> list[str]:
    rest = text.strip().split(maxsplit=1)
    try:
        words = shlex.split((rest[1] if len(rest) > 1 else '').translate(QUOTES))
    except ValueError as e:
        raise Refused(f'I couldn\'t read that ({e}).\n{USAGE}') from None
    return [DASHES.get(word[:1], word[:1]) + word[1:] if word[:1] in DASHES else word for word in words]


def _secret_refusal(server_name: str, key: str = 'KEY') -> Refused:
    var = mcp_config.env_name(server_name or 'server', key)
    return Refused('Not added: that looks like it has a secret in it. ' + WHERE_SECRETS_GO.format(var=var)
                   + ' I deleted your message where I could; check it\'s gone.', secret=True)


def _looks_secret(word: str) -> bool:
    return bool(secrets.redact(word)[1]) or bool(LONG_OPAQUE.match(word)) and any(c.isdigit() for c in word) \
        and any(c.isalpha() for c in word)


def _names(items: list[str], separators: str, kind: str, server: str) -> dict[str, str]:
    """--env / --header items as names (or op:// references). A value is a secret: refused."""
    found = {}
    for item in items:
        at = min((item.find(s) for s in separators if s in item), default=-1)
        key, has_value, value = (item[:at], True, item[at + 1:]) if at >= 0 else (item, False, '')
        key, value = key.strip(), value.strip()
        if not key:
            raise Refused(f'Give the {kind} as NAME (or NAME{separators[0]}op://vault/item/field).')
        if has_value and value and not mcp_config.is_reference(value):
            raise _secret_refusal(server, key)
        found[key] = value if has_value else ''
    return found


def _check_command(server_name: str, words: list[str]) -> None:
    for at, word in enumerate(words):
        flag, has_value, value = word.partition('=')
        following = value if has_value else (words[at + 1] if at + 1 < len(words) else '')
        if SECRET_FLAG.match(flag) and following and not following.startswith(('-', *PATHLIKE)) \
                and not mcp_config.is_reference(following):
            raise _secret_refusal(server_name)
        if _looks_secret(word):
            raise _secret_refusal(server_name)


def _check_url(server_name: str, url: str) -> None:
    parts = urlsplit(url)
    if parts.password or any(SECRET_QUERY.search(name) for name, _ in parse_qsl(parts.query, keep_blank_values=True)):
        raise _secret_refusal(server_name)


def parse(text: str, existing=()) -> McpServer:
    """The server a /mcp_add message asks for, or Refused (the reply). existing: the configured servers."""
    # a key's shape anywhere, before anything else looks at it (a reference names a secret, so it isn't one)
    if secrets.redact(REFERENCE_ANYWHERE.sub('', text))[1]:
        raise _secret_refusal('')
    words = _words(text)
    if not words:
        raise Refused(USAGE)
    try:
        args = commands.parse_add(words)
    except McpConfigError as e:
        raise Refused(f'{e}\n{USAGE}') from None
    env = _names(args.env, '=', 'env', args.name)
    headers = _names(args.header, ':=', 'header', args.name)
    _check_command(args.name, list(args.server_command))
    _check_url(args.name, args.url)
    try:
        server = commands.build_server(args, env, headers)
    except McpConfigError as e:
        raise Refused(f'{e}\n{USAGE}') from None
    if any(s.name == server.name for s in existing):
        raise Refused(f'There is already a server named {server.name}. Replacing one isn\'t done from the phone: '
                      f'remove it on the machine first (wraithd mcp remove {server.name}), or pick another name.')
    return server


def package(server: McpServer) -> str:
    """The package a runner (npx, uvx ...) fetches and runs, '' when there isn't one."""
    if server.transport != 'stdio' or server.command.rsplit('/', 1)[-1] not in RUNNERS:
        return ''
    skip = RUNNERS[server.command.rsplit('/', 1)[-1]]
    rest = list(server.args)
    for word in skip:
        if rest[:1] == [word]:
            rest = rest[1:]
    return next((word for word in rest if not word.startswith('-')), '')


def _secret_lines(server: McpServer) -> list[str]:
    refs = dict(server.refs)
    return [f'    {key}: ' + (f'from 1Password {refs[key]}' if key in refs else
                            f'from {mcp_config.env_name(server.name, key)} (the server\'s env file)')
            for key in server.env + server.headers]


def card(server: McpServer) -> str:
    """The confirm card: the exact block that Allow writes."""
    lines = [f'Add MCP server "{server.name}"?']
    if server.transport == 'stdio':
        lines.append(f'  runs: {shlex.join([server.command, *server.args])}')
        if package(server):
            lines.append(f'  package: {package(server)}')
        if server.sandbox:
            lines.append('  sandbox: yes (inside docker)')
    else:
        lines.append(f'  connects to: {server.url}')
    lines.append(f'  provides: {", ".join(server.provides) or "nothing named"}')
    lines.append(f'  tools: {"only " + ", ".join(server.tools) if server.tools else "every tool it lists"}')
    secret_lines = _secret_lines(server)
    if secret_lines:
        lines += ['  secrets (names only):', *secret_lines]
    lines.append('Allow writes it to config.toml, connects once and pins its tools as they are now. '
                 'Only add servers you trust: it runs on your machine with what you give it.')
    return '\n'.join(lines)


def summary(server: McpServer) -> dict:
    """What the approval row keeps (and the web UI shows): the block, with secret names and never values."""
    return {'name': server.name, 'transport': server.transport,
            **({'command': shlex.join([server.command, *server.args])} if server.transport == 'stdio' else
               {'url': server.url}),
            'provides': list(server.provides), 'tools': list(server.tools), 'sandbox': server.sandbox,
            'secrets': list(server.env + server.headers)}


async def add(rt, server: McpServer, connections=McpConnections) -> str:
    """Allow: writes the block (as `wraithd mcp add` does), connects once so its tools are pinned, and logs
    mcp_added. Returns the reply. rt.config is reloaded, so whatever reconnects next sees the server."""
    current = load_config(rt.home)
    if any(s.name == server.name for s in current.mcp_servers):
        return f'Not added: a server named {server.name} appeared meanwhile.'
    pinning.forget(rt.store, server.name)  # anything left from an old server of that name: start a fresh baseline
    mcp_config.write_server(rt.home.config_path, server, server.name)
    rt.config = replace(rt.config, mcp_servers=load_config(rt.home).mcp_servers)
    async with connections([server], store=rt.store) as connected:
        error = connected.errors.get(server.name)
        names = [original for _, original in connected.origin.values()]
        read_only = {connected.origin[name][1] for name, access in connected.access.items()
                     if access.value == 'read'}
    ev.record_event(rt.store, MCP_ADDED, server=server.name, transport=server.transport,
                    tools=0 if error else len(names), connected=error is None)
    if error:
        hint = ''
        if server.env or server.headers:
            hint = (' If a secret is missing, put it in the server\'s env file over SSH ('
                    + ', '.join(mcp_config.env_name(server.name, k) for k in server.env + server.headers
                                if k not in dict(server.refs))
                    + ') and run `wraithd daemon restart`.')
        return (f'Added {server.name}, but it didn\'t connect: {error}.{hint} Its tools are pinned on its first '
                'connect that works.')
    listed = '\n'.join(f'  {name}' + (' (read-only)' if name in read_only else '') for name in names)
    return (f'Added {server.name}. {len(names)} tool{"" if len(names) == 1 else "s"}, pinned as they are now '
            '(a change later asks again until you review it):\n' + (listed or '  (none)')
            + '\nA tool that isn\'t read-only asks before it runs.')
