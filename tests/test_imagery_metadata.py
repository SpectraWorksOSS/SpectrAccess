"""Provider metadata and independent reader oracles; no real image pixels."""

import ast
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import pytest
from pyproj import Transformer

from spectraccess.connectors.landsat_eodag.angles import read_odl, evaluate_angles
from spectraccess.connectors.sentinel2_cdse.imagery import (
    xml, text, parse_product, parse_datation, ephemeris, angle_grids,
    interpolate_grid, imaging_time, spectral_responses, BANDS,
)
from spectraccess.connectors.sentinel2_cdse.processing import processing_steps

HERE = Path(__file__).parent / "fixtures" / "imagery"


def test_pinned_fixture_hashes():
    manifest = json.loads((HERE / "sources.json").read_text())
    for name, source in manifest.items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == source["sha256"], name


@pytest.mark.parametrize("prefix", ["landsat", "landsat9_C2"])
def test_landsat_independent_usgs_c_oracle(prefix):
    metadata = read_odl(HERE / f"{prefix}_ANG.txt")
    oracle = json.loads((HERE / f"{prefix}_oracle.json").read_text())
    assert len(oracle) > 100
    detectors, bands = set(), set()
    for row in oracle:
        actual = evaluate_angles(metadata, row["band"], np.array([[row["line"]]]),
                                 np.array([[row["sample"]]]), height=row["height"]).sel(detector=row["detector"])
        values = [float(actual[k].values.item()) for k in
                  ("view_zenith", "view_azimuth", "sun_zenith", "sun_azimuth")]
        expected = row["angles"]
        expected[1], expected[3] = expected[1] % 360, expected[3] % 360
        np.testing.assert_allclose(values, expected, atol=1e-8, rtol=0)
        # Time has an independent C L1R-line oracle; epoch uses provider fields.
        from datetime import datetime, timezone, timedelta
        epoch = datetime(int(metadata["EPHEMERIS_EPOCH_YEAR"]), 1, 1, tzinfo=timezone.utc) + timedelta(
            days=int(metadata["EPHEMERIS_EPOCH_DAY"])-1, seconds=metadata["EPHEMERIS_EPOCH_SECONDS"])
        p = f"BAND{row['band']:02d}"
        expected_time = epoch.timestamp() + metadata[p+"_START_TIME"] + row["l1r_line"]*metadata[p+"_LINE_TIME"]
        assert actual.time.item() == pytest.approx(expected_time, abs=1e-6, rel=0)
        detectors.add(row["detector"]); bands.add(row["band"])
    assert bands == set(range(1, 10))
    assert len(detectors) >= 10


def pycontrails_parser():
    # Compile the pinned, unmodified function with its two dependencies.
    # Avoid importing an entire aviation package merely to read XML arrays.
    module = ast.parse((HERE / "vendor" / "sentinel_metadata.py").read_text())
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef)
                    and n.name == "parse_viewing_incidence_angle_by_detector")
    namespace = {"ET": ET, "np": np}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "pinned_pycontrails", "exec"), namespace)
    return namespace[function.name]


@pytest.mark.parametrize("date", ["20210507", "20240501"])
def test_s2_band_detector_grids_vs_pycontrails(date):
    path = HERE / f"{date}_MTD_TL.xml"
    ours = angle_grids(xml(path))
    independent = pycontrails_parser()
    checked = 0
    for band, detector, kind in ours:
        if band is None or kind != "zenith":
            continue
        z, a = independent(str(path), str(detector), str(BANDS.index(band)))
        np.testing.assert_array_equal(ours[(band, detector, "zenith")][0], z)
        np.testing.assert_array_equal(ours[(band, detector, "azimuth")][0], a)
        # Bilinear midpoint oracle from the independent provider-array parser.
        finite = np.isfinite(z[:-1, :-1]+z[1:, :-1]+z[:-1, 1:]+z[1:, 1:])
        points = np.argwhere(finite)
        if len(points):
            row, col = points[len(points)//2]
            dy, dx = ours[(band, detector, "zenith")][1:]
            value = interpolate_grid(ours[(band, detector, "zenith")], (row+.5)*dy, (col+.5)*dx)
            assert value == pytest.approx(z[row:row+2, col:col+2].mean(), abs=1e-10)
        checked += 1
    assert checked > 40


@pytest.mark.parametrize("date", ["20210507", "20240501"])
def test_s2_los_band_delay_envelope(date):
    tile = xml(HERE / f"{date}_MTD_TL.xml")
    strip = xml(HERE / f"{date}_MTD_DS.xml")
    grids, datation, eph = angle_grids(tile), parse_datation(strip), ephemeris(strip)
    to_ll = Transformer.from_crs(text(tile, "HORIZONTAL_CS_CODE"), 4326, always_xy=True)
    ulx, uly = float(text(tile, "Geoposition/ULX")), float(text(tile, "Geoposition/ULY"))
    checked = 0
    for det in sorted({d for b, d, _ in grids if b == "B02"}):
        valid = np.ones(grids[("B02", det, "zenith")][0].shape, dtype=bool)
        for band in ("B02", "B03", "B04"):
            valid &= np.isfinite(grids[(band, det, "zenith")][0])
        points = np.argwhere(valid)
        if not len(points):
            continue
        row, col = points[len(points)//2]
        dy, dx = grids[("B02", det, "zenith")][1:]
        lon, lat = to_ll.transform(ulx + col*dx, uly - row*dy)
        times = []
        for band in ("B02", "B03", "B04"):
            time, residual = imaging_time(eph, lon, lat, grids[(band, det, "zenith")][0][row, col],
                                          grids[(band, det, "azimuth")][0][row, col],
                                          anchor=datation[(band, det)]["utc_epoch"])
            assert residual < .05
            times.append(time)
        for i, nominal in ((1, .527), (2, 1.005)):
            assert abs(abs(times[i]-times[0])-nominal) <= .05
        checked += 1
    assert checked >= 3


def esa_workbook(unit="S2B"):
    namespace = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(HERE / "s2_srf_v4.xlsx") as workbook:
        # Sheet order is fixed by the pinned v4.0 workbook (Overview, A, B, C).
        sheet = {"S2A": 2, "S2B": 3, "S2C": 4}[unit]
        root = ET.fromstring(workbook.read(f"xl/worksheets/sheet{sheet}.xml"))
        rows = []
        for row in root.findall("m:sheetData/m:row", namespace)[1:]:
            values = []
            for cell in list(row)[:14]:
                v = cell.find("m:v", namespace)
                values.append(float(v.text) if v is not None else np.nan)
            if len(values) == 14:
                rows.append(values)
        return np.asarray(rows)


@pytest.mark.parametrize("date", ["20210507", "20240501"])
def test_s2_embedded_vs_esa_v4_comparison_record(date):
    product, _, baseline, _, _, _ = parse_product(HERE / f"{date}_MTD_MSIL1C.xml")
    entries = spectral_responses(product, baseline=baseline, unit="S2B", product_id=date)
    table = esa_workbook()
    observations = {}
    for band, ds in entries.items():
        name = ds.band.item()
        external = np.interp(ds.wavelength, table[:, 0], table[:, BANDS.index(name)+1])
        maximum_difference = float(np.max(np.abs(ds.response.values[0]-external)))
        observations[name] = {"equal_at_1e-6": maximum_difference <= 1e-6,
                              "max_absolute_difference": maximum_difference}
    expected = json.loads((HERE / "s2_srf_comparison.json").read_text())[baseline]
    assert observations == expected
    assert len(observations) == 13


def test_sourced_processing_table_never_extrapolates():
    assert processing_steps("05.10", "collection-1")[0]["parameters"]["harmonisation"]
    assert processing_steps("05.10", "operational")[0]["parameters"]["source"].startswith("https:")
    assert processing_steps("99.99", "collection-1") == []
    assert processing_steps("05.10", None) == []


@pytest.mark.parametrize("unit,version,counts", [
    ("LANDSAT_8", "v1.2", [33, 93, 99, 67, 72, 183, 319, 205, 70]),
    ("LANDSAT_9", "v2-1", None),
])
def test_usgs_srf_preserves_native_samples_and_published_spread(unit, version, counts):
    from spectraccess.core.imagery import read_usgs_srf
    ds = read_usgs_srf(unit=unit)
    assert ds.attrs["source_version"] == version
    assert ds.attrs["flight_unit"] == unit
    assert "stddev" in ds
    assert ds.response.attrs["uncertainty_status"] == "unknown"
    if counts is not None:
        assert ds.response.count("wavelength_sample").values.tolist() == counts
        blue = ds.sel(band="B2")
        assert blue.wavelength[0].item() == 436.
        assert blue.response[0].item() == pytest.approx(.00001)
        assert blue.stddev[0].item() == pytest.approx(.000006)
