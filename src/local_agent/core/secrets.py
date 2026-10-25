"""
Secrets never reach the model. The agent works with credentials (model keys, MCP server tokens) but
doesn't need to see one. Yet tool results can contain them: an email with an API key in it, a page that
prints a token, an error message that echoes a header. So token-shaped strings, and the owner's own keys
by exact value, are replaced before a result reaches the model, the event log, or memory.

This is a scan, so it's best effort: it catches known shapes, not every secret. The real protection is
that the agent's tools never hand it credentials in the first place.
"""

import os
import re

# (kind, pattern). For "name = value" pairs, only the value (group 2) is replaced.
PATTERNS = [
    ('private_key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----')),
    ('anthropic_key', re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}')),
    ('openai_key', re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}')),
    ('stripe_key', re.compile(r'\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}')),
    ('github_token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})')),
    ('slack_token', re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{10,}')),
    ('google_api_key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}')),
    ('aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')),
    ('bearer_token', re.compile(r'(?i)\b(bearer\s+)([A-Za-z0-9._~+/-]{20,}=*)')),
    ('credential', re.compile(r'(?i)\b((?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)'
                              r'["\']?\s*[:=]\s*["\']?)([^\s"\',;]{8,})')),
]

# Keys the owner may have in the environment, matched by exact value whatever their shape
ENV_SECRETS = ['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'SERPAPI_KEY', 'TELEGRAM_BOT_TOKEN']
MIN_SECRET_LENGTH = 12  # shorter values would match ordinary text

# Values registered at runtime: keys read from the keychain, MCP server tokens
_registered: set[str] = set()


def register_secret(value: str) -> None:
    """Redacts this exact value from now on. Call it for every credential the process loads."""
    if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH:
        _registered.add(value)


def clear_registered() -> None:
    _registered.clear()


def _owner_secrets() -> list[str]:
    # Read the environment each time: it's a handful of lookups, and a key set after import still counts
    values = _registered | {os.environ.get(name, '') for name in ENV_SECRETS}
    # Longest first, so a key that contains another is replaced whole
    return sorted((v for v in values if len(v) >= MIN_SECRET_LENGTH), key=len, reverse=True)


def redact(text: str) -> tuple[str, list[str]]:
    """The text with secrets replaced by [REDACTED:kind], and the kinds found (each once, in order)."""
    if not text:
        return text, []
    found = []
    for value in _owner_secrets():
        if value in text:
            text = text.replace(value, '[REDACTED:owner_secret]')
            found.append('owner_secret')
    for kind, pattern in PATTERNS:
        if pattern.groups == 2:
            text, count = pattern.subn(lambda m, k=kind: f'{m.group(1)}[REDACTED:{k}]', text)
        else:
            text, count = pattern.subn(f'[REDACTED:{kind}]', text)
        if count:
            found.append(kind)
    return text, list(dict.fromkeys(found))


def redact_text(text: str) -> str:
    return redact(text)[0]
