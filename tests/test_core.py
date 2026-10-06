from __future__ import annotations

import pytest

from spectraccess.core.connector import Connector
from spectraccess.core.fetch import fetch_url
from spectraccess.core.credentials import Credential, CredentialSession


class DemoConnector(Connector):
    def discover(self, **kwargs):
        return ["https://example.test/data.csv"]

    def fetch(self, target, **kwargs):
        return b"value\n1\n"

    def parse(self, raw):
        return raw.decode("utf-8")


def test_connector_run_chains_methods():
    assert DemoConnector().run() == "value\n1\n"


def test_connector_parse_canonical_default_is_loud():
    with pytest.raises(NotImplementedError, match="canonical schema"):
        DemoConnector().parse_canonical(b"raw")


def test_credential_session_uses_explicit_basic_auth():
    session = CredentialSession("cdse", Credential("password", "secret", "user"))
    assert session.session.auth == ("user", "secret")
    assert session.session.trust_env is False


def test_credential_session_sets_bearer_header():
    session = CredentialSession("earthdata", Credential("token", "token-value"))
    assert session.session.headers["Authorization"] == "Bearer token-value"


def test_fetch_url_retries_and_caches(tmp_path, requests_mock):
    url = "https://example.test/file.txt"
    requests_mock.get(url, [{"status_code": 503}, {"text": "ok"}])

    first = fetch_url(url, cache_dir=tmp_path, backoff_seconds=0, retries=2)
    second = fetch_url(url, cache_dir=tmp_path, backoff_seconds=0, retries=2)

    assert first == b"ok"
    assert second == b"ok"
    assert requests_mock.call_count == 2



def test_version_comes_from_pyproject_via_installed_metadata():
    import re
    from pathlib import Path

    import spectraccess

    # Regex, not tomllib: the suite also runs on Python 3.10.
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE).group(1)
    assert spectraccess.__version__ == declared
