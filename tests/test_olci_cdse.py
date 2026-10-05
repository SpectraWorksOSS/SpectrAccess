import io
import json
import shutil
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest
import xarray as xr

from spectraccess.connectors.olci_cdse import OLCICDSEConnector, PRODUCT_TYPE
from spectraccess.connectors.olci_cdse import connector as module

ROOT = Path(__file__).parent / "fixtures/olci_cdse"
PRODUCT = ROOT / "synthetic.SEN3"


def test_iwv_decoding_qa_time_and_bias_are_separate():
    frame = OLCICDSEConnector().parse(str(PRODUCT))
    assert len(frame) == 3
    assert frame.value.iloc[0] == pytest.approx(20.1)
    assert frame.u_independent.iloc[0] == pytest.approx(.6)
    assert frame.uncertainty_k.iloc[0] == 1
    assert frame.units.eq("kg m-2").all()
    assert frame.correlation_groups.iloc[0] == ["G-940-SURFACE"]
    assert frame.published_bias.iloc[0]["relative_range"] == [.07,.10]
    assert frame.published_bias.iloc[0]["applied"] is False
    assert frame.algorithm_version.eq("fixture-1").all()
    assert frame.unc_status.tolist() == ["provided","provided","unknown"]
    assert frame.valid_time.iloc[1] > frame.valid_time.iloc[0]
    assert not {"bias","u_bias","prior_state","averaging_kernel","likelihood_family","integration_start","footprint_geometry"}.intersection(frame.columns)


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
    def download(raw,path,options):
        assert options["credentials"] is credentials
        assert "filter_pattern" in options
        shutil.copytree(PRODUCT,Path(path)/"synthetic.SEN3")
        return "synthetic.SEN3"
    credentials = object()
    monkeypatch.setattr(module,"download_feature",download)
    path = connector.fetch(target,dest=tmp_path,credentials=credentials)
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
        OLCICDSEConnector().fetch(target,dest=tmp_path,credentials=object())
    assert "fixture-secret" not in str(error.value)


def test_missing_time_and_qa_fail_loudly(tmp_path):
    product = tmp_path/"synthetic.SEN3"
    shutil.copytree(PRODUCT,product)
    with xr.open_dataset(product/"lqsf.nc") as ds:
        bad = ds.load()
    bad.LQSF.attrs.clear()
    bad.to_netcdf(product/"lqsf.nc",engine="h5netcdf")
    with pytest.raises(ValueError,match="QA"):
        OLCICDSEConnector().parse(str(product))
