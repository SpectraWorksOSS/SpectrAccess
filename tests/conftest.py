import pytest
import keyring
from keyring.backends.fail import Keyring


@pytest.fixture(autouse=True)
def isolated_keyring(monkeypatch):
    """Unit tests never read or mutate a contributor's real credentials."""
    monkeypatch.setattr(keyring, "get_keyring", lambda: Keyring())
    monkeypatch.setattr(keyring, "get_credential", lambda *_args: None)


@pytest.fixture
def esa_srf_path():
    from pathlib import Path
    path = Path(__file__).parent / "fixtures" / "imagery" / ".cache" / "s2_srf_v4.xlsx"
    if not path.exists():
        pytest.skip("ESA SRF document absent; run python tests/fixtures/imagery/fetch_esa_srf.py")
    from spectraccess.core.imagery import read_esa_srf
    read_esa_srf(path, unit="S2B")  # Verify identity before any independent oracle reads.
    return path
