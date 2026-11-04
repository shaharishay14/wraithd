import sys

import pytest

from tests.engines import fake_sdk


@pytest.fixture
def claude_sdk(monkeypatch):
    """Installs a fake claude_agent_sdk; call it with scripts (one per query()) to get the module."""
    def install(*scripts):
        fake = fake_sdk.module(*scripts)
        monkeypatch.setitem(sys.modules, 'claude_agent_sdk', fake)
        return fake
    return install
