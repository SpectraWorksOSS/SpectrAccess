import pytest
import keyring
from keyring.backends.fail import Keyring


@pytest.fixture(autouse=True)
def isolated_keyring(monkeypatch):
    """Unit tests never read or mutate a contributor's real credentials."""
    monkeypatch.setattr(keyring, "get_keyring", lambda: Keyring())
    monkeypatch.setattr(keyring, "get_credential", lambda *_args: None)
