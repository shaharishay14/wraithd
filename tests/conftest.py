import pytest

from local_agent.core import gate, secrets
from local_agent.store import SQLiteStore
from tests.scripted import recorder


@pytest.fixture
def store():
    store = SQLiteStore(':memory:')
    yield store
    store.close()


@pytest.fixture(autouse=True)
def test_builtins(monkeypatch):
    """
    get_weather and calculate: two read-only built-ins for the tests to call (the recorded runs use them too).
    The real built-in tools arrive in Phase 3.
    """
    monkeypatch.setattr(gate, 'BUILTIN_TOOL_ACCESS', {**gate.BUILTIN_TOOL_ACCESS,
                                                      'get_weather': gate.ToolAccess.READ,
                                                      'calculate': gate.ToolAccess.READ})


@pytest.fixture(autouse=True)
def no_owner_secrets(monkeypatch):
    """Keys in the developer's environment must not change what tests redact."""
    for name in secrets.ENV_SECRETS:
        monkeypatch.delenv(name, raising=False)
    secrets.clear_registered()
    yield
    secrets.clear_registered()


@pytest.fixture
def events():
    return recorder()


@pytest.fixture
def fast_approvals(monkeypatch):
    """Approvals poll quickly and time out fast, so tests don't wait 60 seconds."""
    monkeypatch.setattr(gate, 'APPROVAL_POLL_SECONDS', 0.01)
    monkeypatch.setattr(gate, 'APPROVAL_TIMEOUT_SECONDS', 0.2)
