"""Regression coverage for credential origin and operation lifetime."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from spectraccess.core.credentials import Credential, CredentialSession
from spectraccess.connectors.landsat_eodag import connector as landsat


@pytest.mark.parametrize("origin,destination", [
    ("https://data.example:443/file", "https://data.example:8443/file"),
    ("https://data.example/file", "https://urs.earthdata.nasa.gov:8443/login"),
    ("https://urs.earthdata.nasa.gov/login", "https://urs.earthdata.nasa.gov:8443/login"),
])
def test_redirect_to_nondefault_port_strips_auth(origin, destination, requests_mock):
    requests_mock.get(origin, status_code=302, headers={"Location": destination})
    requests_mock.get(destination, content=b"fixture")
    with CredentialSession("earthdata", Credential("password", "private-password", "account")) as session:
        session.get(origin)
    assert "Authorization" not in requests_mock.last_request.headers


def test_redirect_default_port_is_same_origin(requests_mock):
    requests_mock.get("https://data.example/file", status_code=302,
                      headers={"Location": "https://data.example:443/next"})
    requests_mock.get("https://data.example:443/next", content=b"fixture")
    with CredentialSession("earthdata", Credential("token", "private-token")) as session:
        session.get("https://data.example/file")
    assert requests_mock.last_request.headers["Authorization"] == "Bearer private-token"


@pytest.mark.parametrize("source_kind", ["callable", "credential", "explicit"])
def test_landsat_constructor_never_resolves_or_copies_credentials(source_kind):
    calls = []
    updates = []
    gateway = SimpleNamespace(update_providers_config=lambda **kwargs: updates.append(kwargs),
                              set_preferred_provider=lambda *args: None)
    credential = Credential("password", "private-password", "account")
    def source():
        calls.append(True)
        return credential
    options = {"credentials": source if source_kind == "callable" else credential}
    if source_kind == "explicit":
        options = {"username": "account", "password": "private-password"}
    landsat.LandsatEodagConnector(gateway=gateway, **options)
    assert calls == []
    assert updates == []


@pytest.mark.parametrize("operation", ["search", "fetch"])
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("source_kind", ["callable", "explicit"])
def test_landsat_operation_releases_all_config_copies(operation, fails, source_kind, tmp_path, monkeypatch):
    gateway = landsat._gateway()
    credential = Credential("password", "private-password", "account")
    options = {"credentials": lambda: credential} if source_kind == "callable" else {
        "username": "account", "password": "private-password"}
    connector = landsat.LandsatEodagConnector(gateway=gateway, **options)
    copied = []
    raw = SimpleNamespace(downloader=None, downloader_auth=None)
    archive = tmp_path / "fixture.tar.gz"
    archive.write_bytes(b"fixture")
    def client(*args, **kwargs):
        provider = gateway._providers["usgs"]
        assert provider.config.api.credentials["password"] == "private-password"
        plugin = next(gateway._plugins_manager.get_search_plugins(provider="usgs"))
        copied.extend([provider.config.api.credentials, plugin.config.credentials])
        raw.downloader = SimpleNamespace(config=deepcopy(plugin.config))
        raw.downloader.config.credentials["api_key"] = "issued-copy"
        copied.append(raw.downloader.config.credentials)
        # Retained plugin caches and returned-product downloaders can own copies.
        gateway._plugins_manager._built_plugins_cache[("usgs", "copy", "")] = raw.downloader
        if fails:
            raise RuntimeError("mock provider error")
        return [] if operation == "search" else str(archive)
    monkeypatch.setattr(gateway, "search", client)
    monkeypatch.setattr(gateway, "download", client)
    def run():
        if operation == "search":
            connector.discover(bbox=(-1, -1, 1, 1))
        else:
            connector.fetch(SimpleNamespace(raw=raw, title="fixture"), dest=tmp_path)
    if fails:
        with pytest.raises((landsat.LandsatProviderError, landsat.LandsatDownloadError)):
            run()
    else:
        run()
    assert not gateway.providers["usgs"].config.api.credentials
    assert copied and all(not value for value in copied)


def test_post_auth_usgs_error_scrubs_issued_key_after_logout(monkeypatch):
    from usgs import api, USGSError
    from eodag.utils.exceptions import RequestError
    issued_key = "issued-private-api-key"
    password = "private-application-password"
    monkeypatch.setattr(api, "login", lambda *args, **kwargs: {"data": issued_key})
    def client(**kwargs):
        memory = landsat._eodag_usgs_api.api
        memory.login("account", password)
        try:
            raise USGSError(f"request failed with {issued_key} and {password}")
        except USGSError as exc:
            memory.logout()
            raise RequestError.from_error(exc) from exc
    gateway = landsat._gateway()
    monkeypatch.setattr(gateway, "search", client)
    connector = landsat.LandsatEodagConnector(
        gateway=gateway, credentials=lambda: Credential("password", password, "account"))
    with pytest.raises(landsat.LandsatProviderError) as caught:
        connector.discover(bbox=(-1, -1, 1, 1))
    assert issued_key not in str(caught.value)
    assert password not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
