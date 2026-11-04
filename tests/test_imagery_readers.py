"""Only tiny synthetic rasters; provider fixtures contain metadata, no imagery."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest
import xarray as xr

from spectraccess.core.imagery import validate, write_netcdf, open_netcdf
from spectraccess.connectors.landsat_eodag.imagery import read_imagery as read_landsat
from spectraccess.connectors.sentinel2_cdse.imagery import read_imagery as read_s2
from spectraccess.connectors.sentinel2_cdse.imagery import xml, text
from spectraccess.connectors.emit_earthaccess.imagery import read_imagery as read_emit

FIXTURES = Path(__file__).parent / "fixtures" / "imagery"


def test_local_readers_without_archive_clients(tmp_path):
    # A fresh interpreter catches eager package imports even when CI installed
    # the clients. Exercise actual synthetic reads, not only symbol imports.
    script = '''
import importlib.abc
from pathlib import Path
import runpy
import sys

class NoArchiveClients(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"earthaccess", "eodag", "cdsetool"}:
            raise ModuleNotFoundError(fullname)

sys.meta_path.insert(0, NoArchiveClients())
from spectraccess.connectors.landsat_eodag import read_imagery
from spectraccess.connectors.sentinel2_cdse import read_imagery
from spectraccess.connectors.emit_earthaccess import read_imagery, EMITEarthaccessConnector
tests = runpy.run_path(sys.argv[1])
root = Path(sys.argv[2])
for name, args in [
    ("test_landsat_counts_and_coefficient_layers_missing_ang", ()),
    ("test_s2_quantity_layers_and_sun_assumption", (True,)),
    ("test_emit_raw_pixels_time_srf_and_unknown_conversion", ()),
]:
    directory = root / name
    directory.mkdir()
    tests[name](directory, *args)
try:
    EMITEarthaccessConnector().discover()
except ImportError as exc:
    assert "spectraccess[emit]" in str(exc)
else:
    raise AssertionError("discovery must request the optional client")
print("Three local readers passed; discovery install hint passed")
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve()), str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def raster(path, values, *, crs="EPSG:32631", origin=(600000, 5700000), resolution=10):
    import rasterio
    from rasterio.transform import from_origin
    with rasterio.open(path, "w", driver="GTiff", width=values.shape[1], height=values.shape[0],
                       count=1, dtype=values.dtype, crs=crs,
                       transform=from_origin(*origin, resolution, resolution)) as dst:
        dst.write(values, 1)


def test_landsat_counts_and_coefficient_layers_missing_ang(tmp_path):
    mtl = '\n'.join([
        'SPACECRAFT_ID = "LANDSAT_9"', 'PROCESSING_LEVEL = "L1TP"',
        'FILE_NAME_BAND_2 = "B2.TIF"', 'REFLECTANCE_MULT_BAND_2 = 0.00002',
        'REFLECTANCE_ADD_BAND_2 = -0.1', 'RADIANCE_MULT_BAND_2 = 0.01',
        'RADIANCE_ADD_BAND_2 = -1.0', 'EARTH_SUN_DISTANCE = 1.01',
    ])
    (tmp_path / "scene_MTL.txt").write_text(mtl)
    raster(tmp_path / "B2.TIF", np.array([[0, 10000], [15000, 20000]], dtype="uint16"))
    tree = read_landsat(tmp_path, bands=[2])
    group = tree["/measurements/B2"]
    assert np.isnan(group.counts[0, 0, 0])
    assert group.toa_reflectance[0, 0, 1] == pytest.approx(.1)
    assert group.radiance[0, 0, 1] == pytest.approx(99.)
    assert group.time.attrs["basis"] == group.view_zenith.attrs["basis"] == "unknown"
    assert tree["/spectral_responses/oli"].attrs["source_version"] == "v2-1"
    assert tree["/spectral_responses/oli"].attrs["representation"] == "tabulated"
    assert validate(tree) == []
    write_netcdf(tree, tmp_path / "scene.nc")
    with open_netcdf(tmp_path / "scene.nc") as reopened:
        xr.testing.assert_identical(tree, reopened)


def safe(tmp_path, *, tile=True):
    shutil.copyfile(FIXTURES / "20240501_MTD_MSIL1C.xml", tmp_path / "MTD_MSIL1C.xml")
    granule = tmp_path / "GRANULE" / "synthetic"
    granule.mkdir(parents=True)
    if tile:
        shutil.copyfile(FIXTURES / "20240501_MTD_TL.xml", granule / "MTD_TL.xml")
    for band in ("B02", "B03", "B04"):
        metadata = xml(FIXTURES / "20240501_MTD_TL.xml")
        origin = (float(text(metadata, "Geoposition/ULX")), float(text(metadata, "Geoposition/ULY")))
        raster(granule / f"test_{band}.jp2", np.array([[0, 2000], [3000, 4000]], dtype="uint16"), origin=origin)
    return tmp_path


@pytest.mark.parametrize("tile", [True, False])
def test_s2_quantity_layers_and_sun_assumption(tmp_path, tile):
    tree = read_s2(safe(tmp_path, tile=tile))
    assert validate(tree) == []
    group = tree["/measurements/r10m"]
    assert group.toa_reflectance.shape == (3, 2, 2)
    assert float(group.toa_reflectance[0, 0, 1]) == pytest.approx(.1)
    assert group.radiance.attrs["basis"] == "evaluated_from_provider_model"
    assumptions = json.loads(group.radiance.attrs["assumptions"])
    assert assumptions[0]["basis"] == "operator_declared"
    assert json.loads(group.radiance.attrs["conversion"])["exactness"] == "not_exact"
    if not tile:
        assert group.sun_zenith.attrs["basis"] == "unknown"
        assert np.isnan(group.radiance.values).all()
    assert group.time.attrs["basis"] == "unknown"
    assert len(tree["/spectral_responses"].children) == 13
    write_netcdf(tree, tmp_path / "imagery.nc")
    with open_netcdf(tmp_path / "imagery.nc") as reopened:
        assert validate(reopened) == []


def emit(tmp_path):
    rad = tmp_path / "rad.nc"
    obs = tmp_path / "obs.nc"
    attrs = {"product_version": "V002", "time_coverage_start": "2024-05-01T23:59:50Z", "orbit": 1}
    xr.Dataset({"radiance": (("downtrack", "crosstrack", "bands"), np.ones((3, 4, 2)),
                             {"units": "uW cm-2 sr-1 nm-1"})}, attrs=attrs).to_netcdf(rad, engine="h5netcdf")
    xr.Dataset({"wavelengths": ("bands", [450., 550.], {"units": "nm"}),
                "fwhm": ("bands", [7.5, 7.5], {"units": "nm"})}).to_netcdf(rad, group="sensor_band_parameters", mode="a", engine="h5netcdf")
    lon, lat = np.meshgrid(np.arange(4.)*.001 + 3, np.arange(3.)*.001 + 51)
    xr.Dataset({"lon": (("downtrack", "crosstrack"), lon), "lat": (("downtrack", "crosstrack"), lat),
                "glt_x": (("ortho_y", "ortho_x"), np.ones((4, 5), dtype="int32")),
                "glt_y": (("ortho_y", "ortho_x"), np.ones((4, 5), dtype="int32"))}).to_netcdf(rad, group="location", mode="a", engine="h5netcdf")
    names = ["To-sensor azimuth (degrees)", "To-sensor zenith (degrees)",
             "To-sun azimuth (degrees)", "To-sun zenith (degrees)",
             "UTC Time (decimal hours for mid-line pixels)", "Earth-sun distance (AU)"]
    values = np.zeros((3, 4, 6))
    values[..., 3] = 30.
    values[..., 4] = np.array([23.999, .001, .002])[:, None]
    values[..., 5] = 1.01
    xr.Dataset({"obs": (("downtrack", "crosstrack", "bands"), values)}, attrs=attrs).to_netcdf(obs, engine="h5netcdf")
    xr.Dataset({"observation_bands": ("bands", names)}).to_netcdf(obs, group="sensor_band_parameters", mode="a", engine="h5netcdf")
    return rad, obs


def test_emit_raw_pixels_time_srf_and_unknown_conversion(tmp_path):
    rad, obs = emit(tmp_path)
    with read_emit(rad, observation=obs, bounds=(3.0006, 51.0006, 3.0014, 51.0014)) as tree:
        group = tree["/measurements/swath"]
        assert group.radiance.dims == ("band", "line", "column")
        assert group.radiance.shape == (2, 1, 1)
        assert group.time.dims == ("line",)
        assert group.time.attrs["basis"] == "published"
        assert group.toa_reflectance.attrs["basis"] == "unknown"
        assert group.toa_reflectance.attrs["sun_normalisation"] == "true_rho_without_clip"
        assert tree["/geolocation_lookup/glt_x"].shape == (4, 5)
        assert tree["/spectral_responses/emit"].attrs["shape_basis"] == "operator_declared"
        assert validate(tree) == []
        write_netcdf(tree, tmp_path / "imagery.nc")
    with open_netcdf(tmp_path / "imagery.nc") as reopened:
        assert validate(reopened) == []


def test_emit_declared_spectrum_conversion_and_midnight(tmp_path):
    rad, obs = emit(tmp_path)
    with read_emit(rad, observation=obs, solar_irradiance=[100., 200.], solar_spectrum="synthetic per nm in radiance area units") as tree:
        group = tree["/measurements/swath"]
        expected = np.pi * 1.01**2 / (100. * np.cos(np.radians(30.)))
        assert float(group.toa_reflectance[0, 0, 0]) == pytest.approx(expected)
        assert np.all(np.diff(group.time) > 0)
        assert group.toa_reflectance.attrs["basis"] == "evaluated_from_provider_model"


def test_emit_missing_obs_unknown_without_scene_defaults(tmp_path):
    rad, _ = emit(tmp_path)
    with read_emit(rad) as tree:
        assert tree["/measurements/swath/time"].attrs["basis"] == "unknown"
        assert tree["/measurements/swath/sun_zenith"].attrs["basis"] == "unknown"


def test_s2_refuses_metadata_from_another_grid(tmp_path):
    root = safe(tmp_path)
    raster(root / "GRANULE/synthetic/test_B02.jp2", np.ones((2, 2), dtype="uint16"), origin=(1, 1))
    with pytest.raises(ValueError, match="differs from MTD_TL"):
        read_s2(root)


def test_external_versioned_srf_keeps_separate_identity(tmp_path, esa_srf_path):
    from spectraccess.core.imagery import read_esa_srf
    srfs = read_esa_srf(esa_srf_path, unit="S2B")
    tree = read_s2(safe(tmp_path), external_srfs={"esa_v4": srfs})
    assert tree["/spectral_responses/esa_v4"].attrs["source_version"] == "4.0"
    assert "source_version" not in tree["/spectral_responses/embedded_B02"].attrs
    assert len(tree["/spectral_responses"].children) == 14


def test_gml_detector_polygons_on_native_window(tmp_path):
    from affine import Affine
    from spectraccess.connectors.sentinel2_cdse.detectors import read_gml_mask
    path = tmp_path / "mask.gml"
    path.write_text('''<root xmlns:gml="http://www.opengis.net/gml">
      <feature gml:id="detector_footprint-B02-07">
        <gml:Polygon srsName="EPSG:32631"><gml:exterior><gml:LinearRing>
          <gml:posList>0 0 20 0 20 20 0 20 0 0</gml:posList>
        </gml:LinearRing></gml:exterior></gml:Polygon>
      </feature></root>''')
    result = read_gml_mask(path, shape=(2, 3), transform=Affine(10, 0, 0, 0, -10, 20), crs="EPSG:32631")
    np.testing.assert_array_equal(result, [[7, 7, 0], [7, 7, 0]])


def test_landsat_provider_mean_height_is_an_assumption(tmp_path):
    (tmp_path / "scene_MTL.txt").write_text('''SPACECRAFT_ID = "LANDSAT_9"
PROCESSING_LEVEL = "L1TP"
FILE_NAME_BAND_2 = "B2.TIF"
REFLECTANCE_MULT_BAND_2 = 0.00002
REFLECTANCE_ADD_BAND_2 = -0.1
RADIANCE_MULT_BAND_2 = 0.01
RADIANCE_ADD_BAND_2 = -1.0
''')
    shutil.copyfile(FIXTURES / "landsat9_C2_ANG.txt", tmp_path / "scene_ANG.txt")
    raster(tmp_path / "B2.TIF", np.ones((2, 2), dtype="uint16"))
    tree = read_landsat(tmp_path, bands=[2])
    for name in ("time", "view_zenith", "sun_zenith"):
        assert json.loads(tree[f"/measurements/B2/{name}"].attrs["assumptions"])[0]["assumption_id"] == "landsat_band_mean_height"
    assert validate(tree) == []


def test_emit_site_box_west_of_dateline_keeps_native_indices(tmp_path):
    rad, obs = emit(tmp_path)
    with xr.open_dataset(rad, group="location", engine="h5netcdf") as ds:
        location = ds.load()
    location["lon"].values[:] = [179.999, -179.999, -179.998, -179.997]
    location.to_netcdf(rad, group="location", mode="a", engine="h5netcdf")
    with read_emit(rad, observation=obs,
                   bounds=(-179.9994, 51.0006, -179.9986, 51.0014)) as tree:
        group = tree["/measurements/swath"]
        assert group.radiance.shape == (2, 1, 1)
        np.testing.assert_array_equal(group.column, [1])
        np.testing.assert_array_equal(group.line, [1])
        assert group.longitude.item() == pytest.approx(-179.999)
        assert validate(tree) == []


@pytest.mark.parametrize("order", [("B02", "B03"), ("B03", "B02")])
def test_s2_per_band_descriptor_provenance_survives_concat(tmp_path, order):
    import xml.etree.ElementTree as ET
    root = safe(tmp_path)
    tile_path = root / "GRANULE/synthetic/MTD_TL.xml"
    tile = xml(tile_path)
    for parent in tile.iter():
        for child in list(parent):
            if child.tag == "Viewing_Incidence_Angles_Grids":
                if child.attrib["bandId"] == "1":
                    parent.remove(child)
                elif child.attrib["bandId"] == "2":
                    for values in child.iter("VALUES"):
                        values.text = " ".join(["5"] * len(values.text.split()))
    ET.ElementTree(tile).write(tile_path, encoding="utf-8")
    # Supply a mask only for B03: published availability must survive concat.
    raster(root / "GRANULE/synthetic/MSK_DETFOO_B03.jp2",
           np.ones((2, 2), dtype="uint16"),
           origin=(float(text(tile, "Geoposition/ULX")), float(text(tile, "Geoposition/ULY"))))
    strip = root / "DATASTRIP/synthetic/MTD_DS.xml"
    strip.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "20240501_MTD_DS.xml", strip)
    tree = read_s2(root, bands=order)
    group = tree["/measurements/r10m"]
    for name in ("view_zenith", "view_azimuth"):
        arr = group[name]
        records = json.loads(arr.attrs["per_band_metadata"])
        assert records["B02"]["basis"] == "unknown"
        assert "grids missing" in records["B02"]["reason"]
        assert records["B03"]["basis"] == "interpolated_from_provider_grid"
        assert records["B03"]["source"] == str(tile_path)
        assert np.isfinite(arr.sel(band="B03")).any()
        assert not np.isfinite(arr.sel(band="B02")).any()
    records = json.loads(group.time.attrs["per_band_metadata"])
    assert "grids missing" in records["B02"]["reason"]
    assert records["B03"]["basis"] == "evaluated_from_provider_model"
    assert records["B03"]["source"].startswith("MTD_DS GPS_Point")
    assert json.loads(records["B03"]["assumptions"])[0]["assumption_id"] == "s2_los_time"
    records = json.loads(group.detector_index.attrs["per_band_metadata"])
    assert records["B02"]["basis"] == "unknown"
    assert records["B03"]["basis"] == "published"
    assert records["B03"]["source"].endswith("MSK_DETFOO_B03.jp2")
    assert validate(tree) == []
    write_netcdf(tree, tmp_path / "mixed.nc")
    with open_netcdf(tmp_path / "mixed.nc") as restored:
        for name in ("time", "detector_index", "view_zenith", "view_azimuth"):
            assert restored[f"/measurements/r10m/{name}"].attrs == group[name].attrs


def test_esa_version_rejects_unpinned_workbook(tmp_path):
    from spectraccess.core.imagery import read_esa_srf
    path = tmp_path / "other.xlsx"
    path.write_bytes(b"another workbook issue")
    with pytest.raises(ValueError, match="checksum"):
        read_esa_srf(path, unit="S2B")


def test_s2_stage_describes_only_primary_processing(tmp_path):
    tree = read_s2(safe(tmp_path), bands=["B02"], collection="operational")
    group = tree["/measurements/r10m"]
    chain = json.loads(group.attrs["stage"])["processing_chain"]
    assert [step["kind"] for step in chain] == ["calibration", "harmonisation", "orthorectification"]
    assert all(step["actor"] == "ESA" for step in chain)
    assert "per_band_parameters" in json.loads(group.radiance.attrs["conversion"])
    assumptions = json.loads(group.radiance.attrs["assumptions"])
    assert assumptions[0]["basis"] == "operator_declared"
    assert assumptions[1]["basis"] == "documented"
    assert "s2-products" in assumptions[1]["statement"]
