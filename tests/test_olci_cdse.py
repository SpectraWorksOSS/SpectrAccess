import io
import json
import shutil
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace

import pytest
import xarray as xr

from spectraccess.connectors.olci_cdse import OLCICDSEConnector, PRODUCT_TYPE
from spectraccess.connectors.olci_cdse import connector as module
from spectraccess.core.credentials import Credential, CredentialMissing, CredentialRejected

ROOT = Path(__file__).parent / "fixtures/olci_cdse"
PRODUCT = ROOT / "synthetic.SEN3"


@pytest.fixture(autouse=True)
def isolated_cdse_auth(monkeypatch):
    # CDSETool exchanges tokens during construction, before download_feature.
    monkeypatch.setattr(module, "_ExplicitCredentials", lambda account, secret: SimpleNamespace(
        username=account, password=secret))


def test_iwv_decoding_qa_time_and_bias_are_separate():
    frame = OLCICDSEConnector().parse(str(PRODUCT))
    assert len(frame) == 3
    assert frame.value.iloc[0] == pytest.approx(20.1)
    assert frame.unc_value.iloc[0] == pytest.approx(.6)
    assert frame.unc_k.isna().all()
    assert frame.unc_provider.iloc[0] == "OLCI IWV_unc"
    assert frame.unc_definition.iloc[0] == "Uncertainty estimate for the Integrated water vapour column above the current pixel"
    assert frame.units.eq("kg m-2").all()
    assert frame.published_bias.iloc[0]["relative_range"] == [.07,.10]
    assert frame.published_bias.iloc[0]["applied"] is False
    assert frame.algorithm_version.eq("fixture-1").all()
    assert frame.unc_status.tolist() == ["provided","provided","unknown"]
    assert frame.valid_time.iloc[1] > frame.valid_time.iloc[0]


def test_flagged_rows_and_bbox():
    frame = OLCICDSEConnector().parse(str(PRODUCT),include_flagged=True)
    assert len(frame) == 5
    assert sum(not qa["accepted"] for qa in frame.qa) == 2
    subset = OLCICDSEConnector().parse(str(PRODUCT),bbox=(3.99,51.99,4.01,52.01))
    assert len(subset) == 1


def test_zip_parser_matches_directory():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive,"w") as z:
        for path in PRODUCT.iterdir():
            z.writestr("synthetic.SEN3/"+path.name,path.read_bytes())
    assert len(OLCICDSEConnector().parse(archive.getvalue())) == 3


def test_discovery_queries_correct_collection_and_filters(monkeypatch):
    feature = json.loads((ROOT/"feature.json").read_text())
    query = Mock(return_value=iter([feature]))
    monkeypatch.setattr(module,"query_features",query)
    target = OLCICDSEConnector().discover(bbox=(4.,51.,5.,52.),start=date(2024,5,1),end=date(2024,5,1),limit=1)[0]
    args = query.call_args.args
    assert args[0] == "SENTINEL-3"
    assert args[1]["productType"] == PRODUCT_TYPE
    assert args[1]["contentDateStartLt"] == "2024-05-02"
    assert query.call_args.kwargs["options"]["expand_attributes"] is True
    assert target.raw == feature


def test_fetch_uses_byoc_and_filtered_maintained_client(monkeypatch,tmp_path):
    feature = json.loads((ROOT/"feature.json").read_text())
    monkeypatch.setattr(module,"query_features",Mock(return_value=[feature]))
    connector = OLCICDSEConnector()
    target = connector.discover(bbox=(4,51,5,52),start=date(2024,5,1),end=date(2024,5,1))[0]
    import cdsetool.download as client
    selected = []
    downloaded = []
    real_filter = client.filter_files
    def select(manifest, pattern, exclude=False):
        result = real_filter(manifest, pattern, exclude)
        selected.extend(result)
        return result
    monkeypatch.setattr(client, "filter_files", select)
    def download(url, path, options):
        assert options["credentials"].username == "fixture-account"
        assert options["credentials"].password == "fixture-secret"
        filename = path.name
        downloaded.append(filename)
        path.write_bytes((PRODUCT / filename).read_bytes())
        return True
    credentials = Credential("password", "fixture-secret", "fixture-account")
    monkeypatch.setattr(client, "download_file", download)
    monkeypatch.setattr(module, "download_file", download)
    path = connector.fetch(target,dest=tmp_path,credentials=credentials)
    assert sorted(p.name for p in selected) == sorted(module._FILES[:4])
    assert sorted(p.name for p in path.path.iterdir()) == sorted(module._FILES)
    assert len(downloaded) == 9  # Four manifest-based selections, plus retained manifest.
    frame = connector.parse(path,target=target)
    assert frame.source_url.eq(target.source_url).all()
    assert frame.platform.eq("S3A").all()
    assert frame.collection_version.eq("003").all()
    assert frame.retrieved_at.notna().all()


def test_download_failure_redacts_provider_exception(monkeypatch,tmp_path):
    feature = json.loads((ROOT/"feature.json").read_text())
    monkeypatch.setattr(module,"query_features",Mock(return_value=[feature]))
    target = OLCICDSEConnector().discover(bbox=(4,51,5,52),start=date(2024,5,1),end=date(2024,5,1))[0]
    monkeypatch.setattr(module,"download_feature",Mock(side_effect=RuntimeError("fixture-secret")))
    with pytest.raises(RuntimeError) as error:
        OLCICDSEConnector().fetch(target,dest=tmp_path,credentials=Credential("password", "fixture-secret", "fixture-account"))
    assert "fixture-secret" not in str(error.value)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None


def test_missing_time_and_qa_fail_loudly(tmp_path):
    product = tmp_path/"synthetic.SEN3"
    shutil.copytree(PRODUCT,product)
    with xr.open_dataset(product/"lqsf.nc") as ds:
        bad = ds.load()
    bad.LQSF.attrs.clear()
    bad.to_netcdf(product/"lqsf.nc",engine="h5netcdf")
    with pytest.raises(ValueError,match="QA"):
        OLCICDSEConnector().parse(str(product))


def test_missing_credential_uses_keyring_without_environment_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("CDSE_USERNAME", "fixture-account")
    monkeypatch.setenv("CDSE_PASSWORD", "fixture-secret")
    monkeypatch.setattr("spectraccess.core.credentials._entry", lambda provider: None)
    download = Mock()
    monkeypatch.setattr(module, "download_feature", download)
    with pytest.raises(CredentialMissing):
        OLCICDSEConnector().fetch(None, dest=tmp_path)
    download.assert_not_called()


def test_download_rejected_credentials_are_classified(monkeypatch, tmp_path):
    import requests
    feature = json.loads((ROOT / "feature.json").read_text())
    monkeypatch.setattr(module, "query_features", Mock(return_value=[feature]))
    connector = OLCICDSEConnector(credentials=lambda: Credential("password", "fixture-secret", "fixture-account"))
    target = connector.discover(bbox=(4, 51, 5, 52), start=date(2024, 5, 1), end=date(2024, 5, 1))[0]
    response = requests.Response()
    response.status_code = 401
    monkeypatch.setattr(module, "download_feature", Mock(side_effect=requests.HTTPError("fixture-secret", response=response)))
    with pytest.raises(CredentialRejected) as error:
        connector.fetch(target, dest=tmp_path)
    assert "fixture-secret" not in str(error.value)
    assert error.value.__context__ is None


def test_provider_uncertainty_definition_is_preserved(tmp_path):
    product = tmp_path / "synthetic.SEN3"
    shutil.copytree(PRODUCT, product)
    with xr.open_dataset(product / "iwv.nc") as ds:
        modified = ds.load()
    modified.IWV_unc.attrs["long_name"] = "Fixture provider's exact uncertainty definition"
    modified.to_netcdf(product / "iwv.nc", engine="h5netcdf")
    frame = OLCICDSEConnector().parse(str(product))
    assert frame.unc_definition.eq("Fixture provider's exact uncertainty definition").all()
    assert frame.unc_value.iloc[0] == pytest.approx(.6)
    assert frame.unc_k.isna().all()


def test_unpublished_uncertainty_stays_unknown(tmp_path):
    product = tmp_path / "synthetic.SEN3"
    shutil.copytree(PRODUCT, product)
    with xr.open_dataset(product / "iwv.nc") as ds:
        modified = ds.drop_vars("IWV_unc").load()
    modified.to_netcdf(product / "iwv.nc", engine="h5netcdf")
    frame = OLCICDSEConnector().parse(str(product))
    assert len(frame) == 3
    assert frame.unc_value.isna().all()
    assert frame.unc_status.eq("unknown").all()
    assert frame.unc_definition.isna().all()
    assert frame.unc_k.isna().all()


@pytest.mark.parametrize("variable_name,text_masks", [("LQSF", False), ("WQSF", False), ("LQSF", True)])
def test_source_flag_layout_and_metadata_are_preserved(tmp_path, variable_name, text_masks):
    product = tmp_path / "synthetic.SEN3"
    shutil.copytree(PRODUCT, product)
    with xr.open_dataset(product / "lqsf.nc", decode_cf=False) as ds:
        modified = ds.load().rename({"LQSF": variable_name})
    attrs = modified[variable_name].attrs
    if text_masks:
        attrs["flag_masks"] = " ".join(str(int(mask)) for mask in attrs["flag_masks"])
    attrs["flag_descriptions"] = "Fixture source descriptions preserved exactly"
    modified.to_netcdf(product / "lqsf.nc", engine="h5netcdf")
    frame = OLCICDSEConnector().parse(str(product), include_flagged=True)
    source = frame.qa.iloc[0]
    assert source["flag_variable"] == variable_name
    assert source["flag_attributes"]["flag_meanings"] == attrs["flag_meanings"]
    assert source["flag_attributes"]["flag_descriptions"] == attrs["flag_descriptions"]
    expected = attrs["flag_masks"] if text_masks else attrs["flag_masks"].tolist()
    assert source["flag_attributes"]["flag_masks"] == expected
    assert source["flag_masks"]["WV_FAIL"] == 1 << 11
    assert any("WV_FAIL" in qa["flags"] and not qa["accepted"] for qa in frame.qa)


@pytest.mark.parametrize("layout", ["missing_attributes", "unexpected_variable", "bad_masks"])
def test_flag_layout_error_reports_names_without_values(tmp_path, layout):
    product = tmp_path / "synthetic.SEN3"
    shutil.copytree(PRODUCT, product)
    with xr.open_dataset(product / "lqsf.nc", decode_cf=False) as ds:
        modified = ds.load()
    variable = "LQSF"
    if layout == "missing_attributes":
        modified.LQSF.attrs.clear()
    elif layout == "unexpected_variable":
        modified = modified.rename({"LQSF": "provider_quality"})
        variable = "provider_quality"
    else:
        modified.LQSF.attrs["flag_masks"] = "private-fixture-value"
    modified[variable].attrs["provider_note"] = "private-fixture-value"
    modified.attrs["source_title"] = "private-fixture-value"
    modified.to_netcdf(product / "lqsf.nc", engine="h5netcdf")
    with pytest.raises(ValueError, match="OLCI QA") as error:
        OLCICDSEConnector().parse(str(product))
    message = str(error.value)
    assert variable in message
    assert "provider_note" in message
    assert "source_title" in message
    assert "private-fixture-value" not in message
