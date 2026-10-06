from pathlib import Path
from types import SimpleNamespace
import ast
import traceback

import keyring
import pytest
import requests
from keyring.backend import KeyringBackend
from keyring.credentials import SimpleCredential

from spectraccess.core import credentials as c
from spectraccess.cli import main


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.entries = {}

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def set_password(self, service, username, password):
        self.entries[service, username] = password

    def delete_password(self, service, username):
        del self.entries[service, username]

    def get_credential(self, service, username):
        for (stored_service, account), secret in self.entries.items():
            if stored_service == service and (username is None or username == account):
                return SimpleCredential(account, secret)


@pytest.fixture
def backend(monkeypatch):
    backend = MemoryKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "get_credential", backend.get_credential)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    return backend


def test_resolve_source_rotation_and_repr():
    secret = "private-value"
    credential = c.Credential("token", secret)
    assert c.resolve("ads", credential) is credential
    assert secret not in repr(credential)
    assert secret not in str(credential)
    values = iter([credential, c.Credential("token", "replacement")])
    source = lambda: next(values)
    assert c.resolve("ads", source).secret == secret
    assert c.resolve("ads", source).secret == "replacement"


def test_missing_ignores_environment_and_read_failure(monkeypatch):
    monkeypatch.setenv("ADS_TOKEN", "ignored")
    monkeypatch.setattr(keyring, "get_credential", lambda *args: (_ for _ in ()).throw(RuntimeError("failed")))
    with pytest.raises(c.CredentialMissing, match="spectraccess login ads.*source"):
        c.resolve("ads")


def test_fail_backend_refuses_login_but_accepts_source():
    with pytest.raises(RuntimeError, match="no usable OS keyring.*hand the credential"):
        c.login("ads")
    assert c.resolve("ads", c.Credential("token", "source")).secret == "source"


def test_login_replaces_account_status_and_logout(backend, monkeypatch, capsys):
    inputs = iter(["person", "y", "replacement-person"])
    secrets = iter(["private-one", "private-two"])
    monkeypatch.setattr("builtins.input", lambda *args: next(inputs))
    monkeypatch.setattr(c, "getpass", lambda *args: next(secrets))
    assert main(["login", "cdse"]) == 0
    assert c.resolve("cdse").account == "person"
    assert main(["login", "cdse"]) == 0
    assert backend.entries == {("spectraccess:cdse", "replacement-person"): "private-two"}
    assert main(["status"]) == 0
    text = capsys.readouterr().out + repr(c.status())
    assert "private-one" not in text and "private-two" not in text
    assert "replacement-person" in text
    assert main(["logout", "cdse"]) == 0
    assert not backend.entries


def test_login_declines_replacement(backend, monkeypatch):
    c.store("ads", c.Credential("token", "existing"))
    monkeypatch.setattr("builtins.input", lambda *args: "n")
    monkeypatch.setattr(c, "getpass", lambda *args: pytest.fail("prompted for secret"))
    assert c.login("ads") is False
    assert c.resolve("ads").secret == "existing"


@pytest.mark.parametrize("status", [401, 403, 500])
def test_http_rejection_and_scrubbed_exception_chain(status, requests_mock):
    credential = c.Credential("token", "tiny")
    requests_mock.get("https://example.test/tiny", status_code=status)
    expected = c.CredentialRejected if status in (401, 403) else RuntimeError
    with pytest.raises(expected) as caught:
        c.CredentialSession("earthdata", credential).get("https://example.test/tiny")
    assert "tiny" not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_source_failure_has_no_raw_context():
    def source():
        raise RuntimeError("private-token")
    with pytest.raises(RuntimeError) as caught:
        c.resolve("ads", source)
    assert "private-token" not in str(caught.value)
    assert caught.value.__context__ is None


def test_earthdata_password_redirect_only_authenticates_urs(requests_mock):
    data = "https://data.lpdaac.earthdatacloud.nasa.gov/file.nc"
    urs = "https://urs.earthdata.nasa.gov/oauth/authorize"
    other = "https://other.example/file.nc"
    requests_mock.get(data, status_code=302, headers={"Location": urs})
    requests_mock.get(urs, status_code=302, headers={"Location": other})
    requests_mock.get(other, content=b"fixture")
    session = c.CredentialSession("earthdata", c.Credential("password", "password", "account"))
    assert session.get(data).content == b"fixture"
    assert requests_mock.request_history[1].headers["Authorization"].startswith("Basic ")
    assert "Authorization" not in requests_mock.request_history[2].headers


def test_bearer_not_forwarded_to_other_hosts_or_http(requests_mock):
    requests_mock.get("https://data.example/file", status_code=302, headers={"Location": "http://data.example/file"})
    requests_mock.get("http://data.example/file", content=b"fixture")
    c.CredentialSession("earthdata", c.Credential("token", "private")).get("https://data.example/file")
    assert "Authorization" not in requests_mock.last_request.headers


def test_declared_provider_registry_has_no_credential_fallbacks():
    root = Path(__file__).resolve().parents[1] / "src/spectraccess/connectors"
    declared = {}
    forbidden = ("ADS_TOKEN", "CDSE_USERNAME", "CDSE_PASSWORD", "EARTHDATA_", "RADCALNET_USERNAME", "RADCALNET_PASSWORD", "EODAG__")
    for path in root.glob("*/connector.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        providers = [node.value.value for node in ast.walk(tree)
                     if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "credential_provider" for t in node.targets)
                     and isinstance(node.value, ast.Constant)]
        for provider in providers:
            assert provider in c.PROVIDERS
            declared[provider] = path
            assert "resolve(" in text or "CredentialSession(" in text
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    call = ast.unparse(node.func)
                    if call in ("os.environ.get", "os.getenv"):
                        assert not any(isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith(forbidden) for arg in node.args)
                    assert call not in ("netrc.netrc", "earthaccess.login")
    assert set(declared) == set(c.PROVIDERS)


def test_cams_handoff_rotation_env_ignored_and_rejection(tmp_path, monkeypatch):
    from datetime import datetime
    from spectraccess.connectors.cams import connector as module
    from spectraccess.connectors.cams import CAMSConnector
    received = []
    def client(url, token):
        received.append(token)
        response = requests.Response()
        response.status_code = 403
        raise requests.HTTPError(f"token={token}", response=response)
    monkeypatch.setattr(module, "_cds_client", client)
    monkeypatch.setenv("ADS_TOKEN", "ignored-env")
    values = iter(["first-secret", "second-secret"])
    connector = CAMSConnector(source="ads", cache_dir=tmp_path,
                              credentials=lambda: c.Credential("token", next(values)))
    for _ in range(2):
        with pytest.raises(c.CredentialRejected) as caught:
            connector.resolve(datetime(2024, 1, 1))
        assert caught.value.__context__ is None
    assert received == ["first-secret", "second-secret"]


def test_landsat_ignores_client_environment_and_login_config(tmp_path, monkeypatch):
    from spectraccess.connectors.landsat_eodag.connector import _gateway
    from eodag.api.provider import ProvidersDict
    monkeypatch.setenv("EODAG__USGS__API__CREDENTIALS__USERNAME", "ignored")
    monkeypatch.setenv("EODAG__USGS__API__CREDENTIALS__PASSWORD", "ignored")
    monkeypatch.setenv("EODAG_CFG_FILE", str(tmp_path / "must-not-read.yml"))
    monkeypatch.setattr(ProvidersDict, "update_from_env", lambda self: pytest.fail("client read credential environment"))
    gateway = _gateway()
    gateway.update_providers_config(dict_conf={"usgs": {"api": {"credentials": {
        "username": "explicit-account", "password": "explicit-token"}}}})
    provider = gateway.providers["usgs"]
    assert provider.config.api.credentials["username"] == "explicit-account"
    assert provider.config.api.credentials["password"] == "explicit-token"


def test_landsat_resolves_source_each_operation(monkeypatch):
    from spectraccess.connectors.landsat_eodag import LandsatEodagConnector
    updates = []
    gateway = SimpleNamespace(update_providers_config=lambda **kwargs: updates.append(
                                  kwargs["dict_conf"]["usgs"]["api"]["credentials"]["password"]),
                              set_preferred_provider=lambda *args: None,
                              search=lambda **kwargs: [])
    state = {"secret": "first"}
    source = lambda: c.Credential("password", state["secret"], "account")
    connector = LandsatEodagConnector(gateway=gateway, credentials=source)
    connector.discover(bbox=(-1, -1, 1, 1))
    state["secret"] = "replacement"
    connector.discover(bbox=(-1, -1, 1, 1))
    assert updates == ["first", "replacement"]


def test_radcalnet_keyring_replacement_applies_next_call(backend, requests_mock, monkeypatch):
    from spectraccess.connectors.radcalnet import RadCalNetConnector
    import base64
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", lambda *args: pytest.fail("read .netrc"))
    c.store("radcalnet", c.Credential("password", "first", "account"))
    connector = RadCalNetConnector()
    requests_mock.get(connector.base_url, json=[{"name": "SITE"}])
    assert connector.sites() == ["SITE"]
    c.store("radcalnet", c.Credential("password", "replacement", "account"))
    assert connector.sites() == ["SITE"]
    header = requests_mock.last_request.headers["Authorization"]
    assert base64.b64decode(header.split()[1]).decode() == "account:replacement"


def test_usgs_api_key_is_in_memory_and_login_file_is_ignored(tmp_path, requests_mock, monkeypatch):
    from usgs import api, USGS_API
    from spectraccess.connectors.landsat_eodag import connector as module
    stale = tmp_path / ".usgs"
    stale.write_text('{"apiKey":"stale-token"}')
    monkeypatch.setattr(api, "TMPFILE", str(stale))
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", lambda *args: pytest.fail("read .netrc"))
    requests_mock.post(USGS_API + "/login-token", json={"errorCode": None, "data": "ephemeral-key"})
    requests_mock.post(USGS_API + "/dataset-filters", json={"errorCode": None, "data": []})
    original_api = module._eodag_usgs_api.api
    with module._usgs_memory_session(c.Credential("password", "application-token", "account")):
        module._eodag_usgs_api.api.login("account", "application-token", save=True)
        module._eodag_usgs_api.api.dataset_filters("dataset")
        assert requests_mock.last_request.headers["X-Auth-Token"] == "ephemeral-key"
        assert "X-Auth-Token" not in requests_mock.request_history[0].headers
    assert module._eodag_usgs_api.api is original_api
    assert stale.read_text() == '{"apiKey":"stale-token"}'


@pytest.mark.parametrize("code", [401, 403])
def test_usgs_wrapped_http_rejection_is_preserved(code, requests_mock):
    from usgs import USGS_API
    from spectraccess.connectors.landsat_eodag import connector as module
    requests_mock.post(USGS_API + "/login-token", status_code=code)
    def search(**_kwargs):
        module._eodag_usgs_api.api.login("account", "application-token", save=True)
    gateway = SimpleNamespace(update_providers_config=lambda **kwargs: None,
                              set_preferred_provider=lambda *args: None, search=search)
    connector = module.LandsatEodagConnector(
        gateway=gateway, credentials=c.Credential("password", "application-token", "account"))
    with pytest.raises(c.CredentialRejected) as caught:
        connector.discover(bbox=(-1, -1, 1, 1))
    assert caught.value.__context__ is None


def test_cdse_client_never_reads_netrc_and_exposes_http_rejection(monkeypatch, requests_mock):
    from spectraccess.connectors.sentinel2_cdse import connector as module
    import netrc
    monkeypatch.setattr(netrc, "netrc", lambda *args: pytest.fail("read .netrc"))
    monkeypatch.setattr(module.Credentials, "_Credentials__ensure_tokens", lambda self: None)
    client = module._ExplicitCredentials("account", "password")
    session = client.make_session(client, False, client.RETRIES, None)
    assert session.trust_env is False
    requests_mock.get("https://identity.example/token", status_code=401)
    with pytest.raises(requests.HTTPError) as caught:
        session.get("https://identity.example/token")
    assert caught.value.response.status_code == 401
