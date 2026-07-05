from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

import requests


@dataclass(frozen=True)
class CredentialConfig:
    username_env: str | None = None
    password_env: str | None = None
    api_key_env: str | None = None
    api_key_header: str = "Authorization"


class CredentialSession:
    """Requests session wrapper for BYO credentials.

    Credentials can be supplied explicitly or loaded from environment
    variables. Values are applied to a `requests.Session` and are never logged.
    """

    def __init__(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        api_key_header: str = "Authorization",
        env: Mapping[str, str] | None = None,
        config: CredentialConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._config = config or CredentialConfig(api_key_header=api_key_header)
        self.session = session or requests.Session()

        resolved_username = username or self._from_env(self._config.username_env)
        resolved_password = password or self._from_env(self._config.password_env)
        resolved_api_key = api_key or self._from_env(self._config.api_key_env)

        if resolved_username is not None or resolved_password is not None:
            if not resolved_username or not resolved_password:
                raise ValueError("both username and password are required for basic auth")
            self.session.auth = (resolved_username, resolved_password)

        if resolved_api_key:
            self.session.headers[self._config.api_key_header] = resolved_api_key

    def _from_env(self, name: str | None) -> str | None:
        return self._env.get(name) if name else None

    @classmethod
    def cookie_login_placeholder(cls, *_args: object, **_kwargs: object) -> "CredentialSession":
        """Placeholder for portals requiring browser/cookie login flows."""
        raise NotImplementedError("cookie-based portal login is not implemented")

