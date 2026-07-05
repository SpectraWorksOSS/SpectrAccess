from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import pandas as pd

from spectraccess.core.connector import Connector
from spectraccess.core.session import CredentialConfig, CredentialSession


@dataclass(frozen=True)
class RadCalNetCredentials:
    username_env: str = "RADCALNET_USERNAME"
    password_env: str = "RADCALNET_PASSWORD"


class RadCalNetConnector(Connector):
    """Stub connector for authenticated RadCalNet access."""

    def __init__(self, credentials: RadCalNetCredentials | None = None) -> None:
        creds = credentials or RadCalNetCredentials()
        self.credential_session = CredentialSession(
            config=CredentialConfig(username_env=creds.username_env, password_env=creds.password_env)
        )

    def discover(self, **_kwargs: object) -> list[object]:
        raise NotImplementedError(
            "STOPPED-AT-STUB: RadCalNet live auth fetch -- awaiting account approval"
        )

    def fetch(self, target: object, **_kwargs: object) -> bytes:
        raise NotImplementedError(
            "STOPPED-AT-STUB: RadCalNet live auth fetch -- awaiting account approval"
        )

    def parse(self, raw: bytes | str) -> pd.DataFrame:
        text = _read_text(raw)
        table = pd.read_csv(StringIO(text), comment="#")
        lower_columns = {column.lower().strip(): column for column in table.columns}
        rename = {}
        for standard, candidates in {
            "timestamp": ["timestamp", "date", "time"],
            "site": ["site", "site_id"],
            "wavelength_nm": ["wavelength_nm", "wavelength", "lambda"],
            "reflectance": ["reflectance", "rho"],
            "uncertainty": ["uncertainty", "reflectance_uncertainty"],
        }.items():
            for candidate in candidates:
                source = lower_columns.get(candidate)
                if source is not None:
                    rename[source] = standard
                    break
        parsed = table.rename(columns=rename)
        if "timestamp" in parsed:
            parsed["timestamp"] = pd.to_datetime(parsed["timestamp"], errors="coerce")
        return parsed


def _read_text(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    path = Path(raw)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return raw

