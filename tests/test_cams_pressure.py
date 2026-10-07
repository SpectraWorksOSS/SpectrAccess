from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from spectraccess.connectors.cams import CAMSConnector, CAMSProviderError, parse_surface_pressure
from spectraccess.connectors.cams import connector as module
from spectraccess.core.credentials import Credential, CredentialMissing, CredentialRejected

FIXTURE = Path(__file__).parent/"fixtures/cams_pressure/surface_pressure.nc"
TIME = datetime(2024,5,1,9,tzinfo=timezone.utc)
AREA = (52.5,3.5,51.,5.5)


def test_pressure_canonical_values_and_support():
    frame = parse_surface_pressure(FIXTURE)
    assert len(frame) == 3
    assert frame.value.tolist() == [101000.,99000.,100000.]
    assert frame.quantity.eq("surface_air_pressure").all()
    assert frame.units.eq("Pa").all()
    assert frame.unc_status.eq("unknown").all()
    assert frame.unc_value.isna().all()
    assert frame.support_kind.eq("grid cell").all()
    assert frame.footprint_geometry.iloc[0]["coordinates"][0][0] == [3.625,51.625]


def test_ads_fetch_is_regional_single_epoch_and_preserves_provenance(monkeypatch,tmp_path):
    client = Mock()
    def retrieve(dataset,request,dest):
        assert dataset == module.ADS_DATASET
        assert request == {"variable":["surface_pressure"],"date":["2024-05-01"],"time":["09:00"],"area":list(AREA),"data_format":"netcdf"}
        Path(dest).write_bytes(FIXTURE.read_bytes())
    client.retrieve.side_effect = retrieve
    monkeypatch.setattr(module,"_cds_client",Mock(return_value=client))
    connector = CAMSConnector(credentials=lambda: Credential("token", "fixture-placeholder"))
    result = connector.fetch_surface_pressure(valid_time=TIME,area=AREA,dest=tmp_path/"pressure.nc")
    frame = connector.parse_surface_pressure(result)
    assert frame.source_url.eq(module.ADS_RETRIEVE_URL).all()
    assert frame.retrieved_at.notna().all()
    assert module.ADS_VARIABLES == ("total_aerosol_optical_depth_550nm","total_column_water_vapour","total_column_ozone")


def test_pressure_credentials_and_epoch_validation(monkeypatch,tmp_path):
    monkeypatch.setenv("ADS_TOKEN", "fixture-secret")
    monkeypatch.setattr("spectraccess.core.credentials._entry", lambda provider: None)
    connector = CAMSConnector()
    with pytest.raises(CredentialMissing):
        connector.fetch_surface_pressure(valid_time=TIME,area=AREA,dest=tmp_path/"x.nc")
    with pytest.raises(ValueError,match="three-hour"):
        connector.fetch_surface_pressure(valid_time=TIME.replace(hour=10),area=AREA,dest=tmp_path/"x.nc")


def test_pressure_provider_failure_preserves_previous_file(monkeypatch,tmp_path):
    client = Mock()
    client.retrieve.side_effect = RuntimeError("fixture-secret")
    monkeypatch.setattr(module,"_cds_client",Mock(return_value=client))
    dest = tmp_path/"pressure.nc"
    dest.write_bytes(b"old")
    with pytest.raises(CAMSProviderError) as error:
        CAMSConnector(credentials=Credential("token", "fixture-secret")).fetch_surface_pressure(valid_time=TIME,area=AREA,dest=dest)
    assert "fixture-secret" not in str(error.value)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert dest.read_bytes() == b"old"


@pytest.mark.parametrize("status", [401, 403])
def test_pressure_rejected_credentials(monkeypatch, tmp_path, status):
    import requests
    response = requests.Response()
    response.status_code = status
    client = Mock()
    client.retrieve.side_effect = requests.HTTPError("fixture-secret", response=response)
    monkeypatch.setattr(module, "_cds_client", Mock(return_value=client))
    with pytest.raises(CredentialRejected) as error:
        CAMSConnector(credentials=Credential("token", "fixture-secret")).fetch_surface_pressure(
            valid_time=TIME, area=AREA, dest=tmp_path / "pressure.nc")
    assert "fixture-secret" not in str(error.value)
    assert error.value.__context__ is None
