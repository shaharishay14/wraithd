import sys

import pytest

from tests.engines import fake_sdk


@pytest.fixture(autouse=True)
def no_other_provider(monkeypatch):
    """A developer's Bedrock or Vertex setting must not change what the tests see."""
    for variable in ('CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def claude_sdk(monkeypatch):
    """Installs a fake claude_agent_sdk; call it with scripts (one per query()) to get the module."""
    def install(*scripts):
        fake = fake_sdk.module(*scripts)
        monkeypatch.setitem(sys.modules, 'claude_agent_sdk', fake)
        return fake
    return install
