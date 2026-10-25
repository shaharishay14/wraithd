import keyring
import pytest
from keyring.backend import KeyringBackend

from local_agent import run as run_module
from local_agent.config import HOME_ENV, Home
from local_agent.core import gate, secrets
from local_agent.run import Runtime
from local_agent.store import SQLiteStore
from tests.scripted import recorder


class MemoryKeyring(KeyringBackend):
    """A keychain that lives for one test."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.passwords = {}

    def get_password(self, service, username):
        return self.passwords.get((service, username))

    def set_password(self, service, username, password):
        self.passwords[(service, username)] = password

    def delete_password(self, service, username):
        self.passwords.pop((service, username), None)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No test touches the real ~/.local-agent or the real keychain."""
    monkeypatch.setenv(HOME_ENV, str(tmp_path / 'home'))
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def home():
    home = Home.locate()
    home.ensure()
    return home


@pytest.fixture
def store():
    store = SQLiteStore(':memory:')
    yield store
    store.close()


@pytest.fixture
def make_runtime(monkeypatch):
    """A Runtime on the temp home with scripted models: make_runtime(model, strong=None)."""
    monkeypatch.setattr(run_module, 'RETRY_DELAYS', (0, 0))
    opened = []

    def make(model, strong=None):
        models = {'default': model, **({'strong': strong} if strong else {})}
        rt = Runtime.open(Home.locate(), models=models)
        opened.append(rt)
        return rt

    yield make
    for rt in opened:
        rt.close()


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
    """Keys in the developer's environment must not change what tests redact or which key is used."""
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
