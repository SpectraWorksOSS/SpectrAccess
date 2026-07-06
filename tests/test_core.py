from __future__ import annotations

import pytest

from spectraccess.core.connector import Connector
from spectraccess.core.fetch import fetch_url
from spectraccess.core.session import CredentialConfig, CredentialSession


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


def test_credential_session_uses_env_for_basic_auth():
    session = CredentialSession(
        env={"USER_ENV": "user", "PASS_ENV": "secret"},
        config=CredentialConfig(username_env="USER_ENV", password_env="PASS_ENV"),
    )
    assert session.session.auth == ("user", "secret")


def test_credential_session_requires_complete_basic_auth():
    with pytest.raises(ValueError):
        CredentialSession(username="user")


def test_credential_session_sets_api_key_header():
    session = CredentialSession(api_key="token", api_key_header="X-API-Key")
    assert session.session.headers["X-API-Key"] == "token"


def test_fetch_url_retries_and_caches(tmp_path, requests_mock):
    url = "https://example.test/file.txt"
    requests_mock.get(url, [{"status_code": 503}, {"text": "ok"}])

    first = fetch_url(url, cache_dir=tmp_path, backoff_seconds=0, retries=2)
    second = fetch_url(url, cache_dir=tmp_path, backoff_seconds=0, retries=2)

    assert first == b"ok"
    assert second == b"ok"
    assert requests_mock.call_count == 2

