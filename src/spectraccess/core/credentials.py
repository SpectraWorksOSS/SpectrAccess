"""Provider credentials from the OS keyring or a source supplied by a program."""
from __future__ import annotations

from dataclasses import dataclass, field
from getpass import getpass
from typing import Callable
from urllib.parse import urlparse

import keyring
import requests

PROVIDERS = {
    "cdse": ("password",), "ads": ("token",),
    "earthdata": ("password", "token"), "usgs": ("password",),
    "radcalnet": ("password",),
}


@dataclass(frozen=True)
class Credential:
    kind: str
    secret: str = field(repr=False)
    account: str = "token"


CredentialSource = Credential | Callable[[], Credential]


class CredentialMissing(ValueError):
    def __init__(self, provider: str):
        super().__init__(f"{provider} credentials are required; run spectraccess login {provider}, "
                         "or hand over a credential source in code")


class CredentialRejected(RuntimeError):
    def __init__(self, provider: str, source: str):
        super().__init__(f"{provider} rejected credentials from {source}; "
                         f"run spectraccess login {provider} or update your credential source")


def _provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown credential provider; choose from {', '.join(PROVIDERS)}")
    return f"spectraccess:{provider}"


def _entry(provider: str):
    service = _provider(provider)
    try:
        return keyring.get_credential(service, None)
    except Exception:
        return None


def resolve(provider: str, source: CredentialSource | None = None) -> Credential:
    _provider(provider)
    if source is not None:
        failure = None
        try:
            credential = source() if callable(source) else source
        except Exception:
            failure = RuntimeError(f"{provider} credential source failed")
        if failure is not None:
            raise failure from None
    else:
        entry = _entry(provider)
        if entry is None:
            raise CredentialMissing(provider)
        credential = Credential("token" if entry.username == "token" else "password",
                                entry.password, entry.username)
    if not isinstance(credential, Credential):
        raise TypeError("credential source must return a Credential")
    if credential.kind not in PROVIDERS[provider] or not credential.secret or not credential.account:
        raise ValueError(f"invalid credential for {provider}")
    if credential.kind == "token" and credential.account != "token":
        raise ValueError("token credentials use account 'token'")
    if credential.kind == "password" and credential.account == "token":
        raise ValueError("password account cannot use the reserved name 'token'")
    return credential


def _usable_backend() -> None:
    usable = False
    try:
        usable = keyring.get_keyring().priority >= 1
    except Exception:
        pass
    if not usable:
        raise RuntimeError("no usable OS keyring on this machine; configure a keyring backend "
                           "or hand the credential over in code")


def store(provider: str, credential: Credential) -> None:
    credential = resolve(provider, credential)
    _usable_backend()
    old = _entry(provider)
    failure = None
    try:
        keyring.set_password(_provider(provider), credential.account, credential.secret)
        if old is not None and old.username != credential.account:
            keyring.delete_password(_provider(provider), old.username)
    except Exception:
        failure = RuntimeError(f"could not store {provider} credentials in keyring")
    if failure is not None:
        raise failure from None


def remove(provider: str) -> None:
    entry = _entry(provider)
    failure = None
    if entry is not None:
        try:
            keyring.delete_password(_provider(provider), entry.username)
        except Exception:
            failure = RuntimeError(f"could not remove {provider} credentials from keyring")
    if failure is not None:
        raise failure from None


def status() -> list[dict[str, object]]:
    backend = keyring.get_keyring()
    name = f"{type(backend).__module__}.{type(backend).__name__}"
    rows = []
    for provider in PROVIDERS:
        entry = _entry(provider)
        rows.append(dict(provider=provider, stored=entry is not None,
                         account=entry.username if entry else None, backend=name))
    return rows


def login(provider: str) -> bool:
    _provider(provider)
    _usable_backend()
    if _entry(provider) is not None:
        if input(f"Replace stored {provider} credentials? [y/N] ").strip().lower() != "y":
            return False
    kinds = PROVIDERS[provider]
    kind = kinds[0] if len(kinds) == 1 else input("Credential kind (password/token): ").strip()
    if kind not in kinds:
        raise ValueError(f"unsupported credential kind for {provider}")
    account = "token" if kind == "token" else input("Account: ").strip()
    secret = getpass("Token: " if kind == "token" else "Password/application token: ")
    store(provider, Credential(kind, secret, account))
    return True


logout = remove


def describe_error(exc: BaseException | None, *, secret: str | None = None) -> str:
    if exc is None:
        return "unknown error"
    text = str(exc)
    if secret:
        for part in sorted({secret, *secret.split(":")}, key=len, reverse=True):
            if part:
                text = text.replace(part, "<redacted>")
    text = " ".join(text.split())[:500]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def provider_error(provider: str, source: CredentialSource | None, exc: Exception,
                   credential: Credential, error_type: type[Exception]) -> Exception:
    if isinstance(exc, CredentialRejected):
        return CredentialRejected(provider, "handed-over source" if source is not None else "keyring")
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        code = (getattr(response, "status_code", None) or
                getattr(current, "status_code", None) or getattr(current, "code", None))
        if code in (401, 403):
            return CredentialRejected(provider, "handed-over source" if source is not None else "keyring")
        current = current.__cause__ or current.__context__
    return error_type(describe_error(exc, secret=credential.secret))


class _Session(requests.Session):
    def rebuild_auth(self, prepared_request, response):
        old = urlparse(response.request.url)
        new = urlparse(prepared_request.url)
        if old.scheme == new.scheme == "https" and old.hostname == new.hostname:
            return
        prepared_request.headers.pop("Authorization", None)
        if (self.earthdata and new.scheme == "https" and
                new.hostname == "urs.earthdata.nasa.gov" and self.auth):
            prepared_request.prepare_auth(self.auth)


class CredentialSession:
    """Fresh per-use authenticated session; never consult .netrc."""
    def __init__(self, provider: str, credentials: CredentialSource | None = None):
        self.provider = provider
        self.source = credentials
        self.credential = resolve(provider, credentials)
        self.session = _Session()
        self.session.earthdata = provider == "earthdata"
        self.session.trust_env = False
        if self.credential.kind == "password":
            self.session.auth = (self.credential.account, self.credential.secret)
        else:
            self.session.headers["Authorization"] = f"Bearer {self.credential.secret}"

    def get(self, url: str, **kwargs):
        failure = None
        try:
            response = self.session.get(url, **kwargs)
            response.raise_for_status()
        except Exception as exc:
            failure = provider_error(self.provider, self.source, exc, self.credential, RuntimeError)
        if failure is not None:
            raise failure from None
        return response

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
