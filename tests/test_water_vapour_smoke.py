"""Smoke adapters hand credentials over without contacting providers."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

from spectraccess.core.credentials import Credential

spec = importlib.util.spec_from_file_location("live_smoke", Path(__file__).parents[1] / "scripts/live_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.mark.parametrize("name, variables", [
    ("smoke_olci_cdse", ("CDSE_USERNAME", "CDSE_PASSWORD")),
    ("smoke_cams_pressure", ("ADS_TOKEN",)),
])
def test_missing_smoke_credentials_skip(monkeypatch, capsys, name, variables):
    for variable in variables:
        monkeypatch.delenv(variable, raising=False)
    getattr(smoke, name)()
    assert "SKIP" in capsys.readouterr().out


def test_olci_smoke_hands_over_cdse_credential(monkeypatch):
    import cdsetool.download
    import spectraccess.connectors.olci_cdse as olci
    monkeypatch.setenv("CDSE_USERNAME", "fixture-account")
    monkeypatch.setenv("CDSE_PASSWORD", "fixture-secret")
    target = Mock(product_id="fixture-id", title="fixture.SEN3")
    connector = Mock()
    connector.discover.return_value = [target]
    def make_connector(*, credentials):
        assert credentials() == Credential("password", "fixture-secret", "fixture-account")
        connector.credentials = credentials
        return connector
    monkeypatch.setattr(olci, "OLCICDSEConnector", make_connector)
    def download(url, path, options):
        assert options["credentials"].username == "fixture-account"
        assert options["credentials"].password == "fixture-secret"
        path.write_text("<manifest />")
        return True
    monkeypatch.setattr(cdsetool.download, "download_file", download)
    smoke.smoke_olci_cdse()


def test_pressure_smoke_hands_over_ads_credential(monkeypatch):
    import pandas as pd
    import spectraccess.connectors.cams as cams
    monkeypatch.setenv("ADS_TOKEN", "fixture-token")
    connector = Mock()
    connector.parse_surface_pressure.return_value = pd.DataFrame({"value": [101000.]})
    def make_connector(*, source, credentials):
        assert source == "ads"
        assert credentials() == Credential("token", "fixture-token")
        return connector
    monkeypatch.setattr(cams, "CAMSConnector", make_connector)
    smoke.smoke_cams_pressure()
