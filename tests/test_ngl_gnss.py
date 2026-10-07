import gzip
import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from spectraccess.connectors.ngl_gnss import NGLGNSSConnector, Station, parse_sinex, parse_stations

FIXTURE = Path(__file__).parent / "fixtures/ngl_gnss/ABMF.2008.246.trop"


def test_real_sinex_extract_values_errors_location_and_gradient_warning():
    frame = parse_sinex(FIXTURE.read_text())
    assert len(frame) == 6
    ztd = frame.loc[frame.quantity == "zenith_total_delay"].iloc[0]
    assert ztd.value == pytest.approx(2.6628)
    assert ztd.unc_value == pytest.approx(.0034)
    assert ztd.elevation_m == -25.3
    assert ztd.longitude == pytest.approx(-61.5275278)
    assert str(ztd.valid_time) == "2008-09-02 00:00:00+00:00"
    north = frame.loc[frame.quantity == "tropospheric_gradient_north"].iloc[0]
    east = frame.loc[frame.quantity == "tropospheric_gradient_east"].iloc[0]
    assert north.value == pytest.approx(.00057)
    assert north.unc_value == pytest.approx(.00037)
    assert east.value == pytest.approx(-.00145)
    assert east.unc_value == pytest.approx(.00058)
    assert north.qa["gradient_columns_interchanged"] is True
    assert "GipsyX-2.3" in ztd.algorithm_version
    assert "averaging_kernel" not in frame
    assert "integration_start" not in frame
    assert not frame.quantity.str.contains("water").any()


def test_precise_station_metadata_and_no_swap_option():
    station = parse_stations("ABMF 16.25 -421.5 17.4\n")["ABMF"]
    assert station.longitude == -61.5
    frame = parse_sinex(FIXTURE.read_text(),station=station,correct_gradient_swap=False)
    assert frame.elevation_m.eq(17.4).all()
    assert frame.loc[frame.quantity == "tropospheric_gradient_north","value"].iloc[0] == pytest.approx(-.00145)


def test_ztd_only_and_unknown_sigma():
    text = FIXTURE.read_text()
    head = text.split("+TROP/SOLUTION")[0]
    frame = parse_sinex(head+"+TROP/SOLUTION\n*SITE ___EPOCH____ TROTOT _SIG\n ABMF 08:246:00000 2662.8 -999.0\n-TROP/SOLUTION\n")
    assert frame.quantity.tolist() == ["zenith_total_delay"]
    assert frame.unc_status.tolist() == ["unknown"]


def test_single_published_gradient_keeps_corrected_direction():
    head = FIXTURE.read_text().split("+TROP/SOLUTION")[0]
    frame = parse_sinex(head+"+TROP/SOLUTION\n*SITE ___EPOCH____ TROTOT _SIG TGETOT _SIG\n ABMF 08:246:00000 2662.8 3.4 0.57 0.37\n-TROP/SOLUTION\n")
    assert frame.quantity.tolist() == ["zenith_total_delay", "tropospheric_gradient_north"]


@pytest.mark.parametrize("change",[lambda t:t.replace("-TROP/SOLUTION", ""),lambda t:t.replace("TROTOT","UNKNOWN"),lambda t:t.replace("08:246:00000","08:999:00000")])
def test_malformed_sinex_fails(change):
    with pytest.raises(ValueError):
        parse_sinex(change(FIXTURE.read_text()))


def test_public_zip_fetch_and_run_provenance(requests_mock):
    station = Station("ABMF",16.25,-61.5,17.4)
    connector = NGLGNSSConnector()
    target = connector.discover(station=station,day=date(2008,9,2))[0]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive,"w") as z:
        z.writestr("ABMF.2008.246.trop.gz",gzip.compress(FIXTURE.read_bytes()))
    requests_mock.get(target.source_url,content=archive.getvalue())
    frame = connector.run(station=station,day=date(2008,9,2),canonical=True)
    assert frame.source_url.eq(target.source_url).all()
    assert frame.retrieved_at.notna().all()
    assert requests_mock.last_request.headers.get("Authorization") is None


def test_missing_day_and_download_cap(requests_mock):
    connector = NGLGNSSConnector()
    target = connector.discover(station=Station("ABMF",16.,-61.,0),day=date(2008,9,3))[0]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive,"w") as z:
        z.writestr("ABMF.2008.246.trop.gz",gzip.compress(FIXTURE.read_bytes()))
    requests_mock.get(target.source_url,content=archive.getvalue())
    with pytest.raises(ValueError,match="daily member"):
        connector.fetch(target)
    connector.max_bytes = 10
    with pytest.raises(ValueError,match="max_bytes"):
        connector.fetch(target)
