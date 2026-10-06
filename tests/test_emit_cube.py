"""Synthetic netCDF4 groups, no provider data or network."""

from dataclasses import replace

import numpy as np
import pytest
import xarray as xr

pytest.importorskip("earthaccess")
pytest.importorskip("h5netcdf")

from spectraccess.connectors.emit_earthaccess import EMITEarthaccessConnector, read_cube
from test_emit_earthaccess import _granule
from spectraccess.connectors.emit_earthaccess.connector import _target_from_granule


V001_MASK_LABELS = [
    "Cloud flag", "Cirrus flag", "Standing water flag", "Spacecraft flag",
    "Dilated cloud mask", "AOD550", "H2O (g cm-2)", "Aggregate bad data flag",
]


def write_product(path, version="001", variable="reflectance", bands=10, offset=0,
                  mask_labels=True):
    dims = ("downtrack", "crosstrack", "bands")
    values = np.arange(4 * 3 * bands, dtype="float32").reshape(4, 3, bands) / 100
    values[0, 0, 0] = -9999
    values[0, 0, 1] = -0.01
    root = xr.Dataset({variable: (dims, values)}, attrs={"product_version": "V" + version})
    root[variable].attrs.update(_FillValue=-9999., units="1")
    if variable == "obs":
        root[variable].attrs["units"] = "degree"
    if variable == "mask" and version == "002":
        root["water_vapor"] = (("downtrack", "crosstrack"), np.ones((4, 3)))
        root["water_vapor"].attrs["units"] = "g cm-2"
    root.to_netcdf(path, engine="h5netcdf")
    if variable in {"reflectance", "reflectance_uncertainty"}:
        params = xr.Dataset({
            "wavelengths": ("bands", np.arange(bands) * 10. + 400 + offset, {"units": "nm"}),
            "fwhm": ("bands", np.ones(bands) * 7.5, {"units": "nm"}),
            "good_wavelengths": ("bands", np.array([1, 0] + [1] * (bands - 2), dtype="int8")),
        })
    else:
        names = [f"channel_{i}" for i in range(bands)]
        if variable == "obs":
            names[:4] = ["Solar Zenith", "Solar Azimuth", "View Zenith", "View Azimuth"]
        elif variable == "mask" and version == "001":
            names = V001_MASK_LABELS
        params = xr.Dataset({variable + "_bands": ("bands", names)})
    if variable != "mask" or mask_labels:
        params.to_netcdf(path, group="sensor_band_parameters", mode="a", engine="h5netcdf")
    if variable == "reflectance":
        fill = -9999 if version == "001" else 0
        glt = np.arange(20, dtype="int32").reshape(4, 5)
        glt[0, 0] = fill
        xr.Dataset({
            "lat": (("downtrack", "crosstrack"), np.ones((4, 3)), {"units": "degrees_north"}),
            "lon": (("downtrack", "crosstrack"), np.ones((4, 3)) * 2, {"units": "degrees_east"}),
            "glt_x": (("ortho_y", "ortho_x"), glt, {"_FillValue": fill}),
            "glt_y": (("ortho_y", "ortho_x"), glt, {"_FillValue": fill}),
        }).to_netcdf(path, group="location", mode="a", engine="h5netcdf")
    return path


@pytest.mark.parametrize("version", ["001", "002"])
def test_raw_cube(version, tmp_path):
    rfl = write_product(tmp_path / "rfl.nc", version)
    unc = write_product(tmp_path / "unc.nc", version, "reflectance_uncertainty")
    mask = write_product(tmp_path / "mask.nc", version, "mask", bands=8 if version == "001" else 6)
    obs = write_product(tmp_path / "obs.nc", version, "obs", bands=4)
    with read_cube(rfl, uncertainty=unc, mask=mask, observation=obs) as cube:
        assert cube.reflectance.dims == ("downtrack", "crosstrack", "bands")
        assert cube.reflectance_uncertainty.dims == cube.reflectance.dims
        assert np.isnan(cube.reflectance[0, 0, 0])
        assert np.isnan(cube.reflectance_uncertainty[0, 0, 0])
        assert cube.reflectance[0, 0, 1] == pytest.approx(-0.01)
        assert cube.reflectance_uncertainty[0, 0, 1] == pytest.approx(-0.01)
        assert cube.good_wavelengths[1] == 0
        assert cube.glt_x.dtype == np.dtype("int32")
        assert cube.glt_x[0, 0] == (-9999 if version == "001" else 0)
        assert cube.glt_x.attrs["_FillValue"] == (-9999 if version == "001" else 0)
        assert cube.attrs["product_version"] == "V" + version
        np.testing.assert_array_equal(cube.wavelengths, np.arange(10) * 10 + 400)
        assert cube.wavelengths.attrs["units"] == "nm"
        assert cube.lat.shape == cube.lon.shape == (4, 3)
        assert cube.obs.dims == ("downtrack", "crosstrack", "observation_bands")
        assert cube.obs_obs_bands.values[0] == "Solar Zenith"
        assert cube.obs.attrs["units"] == "degree"
        assert np.isnan(cube["mask"][0, 0, 0])
        assert cube["mask"][0, 0, 1] == pytest.approx(-0.01)
        assert cube["mask"][0, 0, 2] == pytest.approx(0.02)
        assert "for each channel" in cube.reflectance_uncertainty.attrs["provider_definition"]
        assert "one standard deviation" in cube.reflectance_uncertainty.attrs["long_name"]
        if version == "001":
            assert cube.aerosol_optical_depth[0, 0] == pytest.approx(0.05)
            assert cube.water_vapor[0, 0] == pytest.approx(0.06)
            assert cube.water_vapor[0, 0] != cube["mask"][0, 0, 7]
            assert cube.aerosol_optical_depth.attrs["alias_source"] == "provider mask_bands label"
            assert cube.water_vapor.attrs["alias_source"] == "provider mask_bands label"
        else:
            assert cube.water_vapor.attrs["units"] == "g cm-2"
            assert "aerosol_optical_depth" not in cube


def test_per_granule_parameters_and_optional_files(tmp_path):
    for offset in (0, 7):
        path = write_product(tmp_path / f"rfl{offset}.nc", offset=offset)
        with EMITEarthaccessConnector.read_cube(path) as cube:
            assert cube.wavelengths[0] == 400 + offset
            assert "reflectance_uncertainty" not in cube
            assert "mask" not in cube and "obs" not in cube
    with pytest.raises(FileNotFoundError):
        read_cube(path, uncertainty=tmp_path / "missing.nc")


@pytest.mark.parametrize("problem", ["version", "bands", "pixels", "scene"])
def test_incompatible_companions_rejected(problem, tmp_path):
    rfl = write_product(tmp_path / "rfl.nc")
    unc = write_product(tmp_path / "unc.nc", "002" if problem == "version" else "001",
                        "reflectance_uncertainty", bands=9 if problem == "bands" else 10)
    if problem in {"pixels", "scene"}:
        with xr.open_dataset(unc, engine="h5netcdf", mask_and_scale=False) as source:
            altered = source.load()
        if problem == "pixels":
            altered = altered.isel(downtrack=slice(0, 3))
        else:
            altered.attrs["orbit"] = "other"
            with xr.open_dataset(rfl, engine="h5netcdf", mask_and_scale=False) as source:
                primary = source.load()
            primary.attrs["orbit"] = "original"
            primary.to_netcdf(rfl, mode="a", engine="h5netcdf")
        # Replace root only: validation fails before companion groups are read.
        altered.to_netcdf(unc, engine="h5netcdf")
    with pytest.raises(ValueError, match="differs|does not match"):
        read_cube(rfl, uncertainty=unc)


def test_metadata_methods_share_file_version_parser(tmp_path):
    path = write_product(tmp_path / "rfl.nc")
    target = _target_from_granule(_granule(), expected_product="EMITL2ARFL")
    connector = EMITEarthaccessConnector()
    assert connector.parse(str(path), target=target).loc[0, "version"] == "001"
    assert len(connector.parse_canonical(str(path), target=target)) == 3
    wrong = replace(target, version="002")
    for method in (connector.parse, connector.parse_canonical, connector.read_cube):
        with pytest.raises(ValueError, match="differs from CMR"):
            method(str(path), target=wrong)


def test_open_does_not_read_reflectance_values(tmp_path, monkeypatch):
    from xarray.backends.h5netcdf_ import H5NetCDFArrayWrapper

    path = write_product(tmp_path / "rfl.nc")
    original = H5NetCDFArrayWrapper.__getitem__
    reads = []

    def record_read(self, key):
        if self.variable_name == "reflectance":
            reads.append(key)
        return original(self, key)

    monkeypatch.setattr(H5NetCDFArrayWrapper, "__getitem__", record_read)
    with read_cube(path) as cube:
        assert reads == []
        cube.reflectance.isel(downtrack=0, crosstrack=0).values
        assert len(reads) == 1


def test_conflicting_raw_coordinates_do_not_reindex(tmp_path):
    rfl = write_product(tmp_path / "rfl.nc")
    unc = write_product(tmp_path / "unc.nc", variable="reflectance_uncertainty")
    for path, coords in ((rfl, [0, 1, 2, 3]), (unc, [1, 2, 3, 4])):
        with xr.open_dataset(path, engine="h5netcdf", mask_and_scale=False) as source:
            altered = source.load().assign_coords(downtrack=coords)
        altered.to_netcdf(path, mode="a", engine="h5netcdf")
    with pytest.raises(ValueError, match="align|index"):
        read_cube(rfl, uncertainty=unc)


def test_unknown_version_rejected_without_filename_guess(tmp_path):
    path = write_product(tmp_path / "EMIT_L2A_RFL_001.nc", version="003")
    with pytest.raises(ValueError, match="unsupported EMIT product version"):
        read_cube(path)


def test_v001_mask_aliases_follow_labels_not_positions(tmp_path):
    rfl = write_product(tmp_path / "rfl.nc")
    mask = write_product(tmp_path / "mask.nc", variable="mask", bands=8)
    # Reorder both value channels and labels, changing case to exercise matching.
    order = [5, 6, 0, 1, 2, 3, 4, 7]
    with xr.open_dataset(mask, engine="h5netcdf", mask_and_scale=False) as source:
        reordered = source.isel(bands=order).load()
    reordered.to_netcdf(mask, mode="a", engine="h5netcdf")
    labels = [V001_MASK_LABELS[index].swapcase() for index in order]
    xr.Dataset({"mask_bands": ("bands", labels)}).to_netcdf(
        mask, group="sensor_band_parameters", mode="a", engine="h5netcdf",
    )
    with read_cube(rfl, mask=mask) as cube:
        assert cube.aerosol_optical_depth[0, 0] == pytest.approx(0.05)
        assert cube.water_vapor[0, 0] == pytest.approx(0.06)
        assert cube.aerosol_optical_depth.attrs["alias_source"] == "provider mask_bands label"
        assert cube.water_vapor.attrs["alias_source"] == "provider mask_bands label"


@pytest.mark.parametrize("empty_group", [False, True])
def test_v001_mask_aliases_fall_back_only_without_labels(tmp_path, empty_group):
    rfl = write_product(tmp_path / "rfl.nc")
    mask = write_product(tmp_path / "mask.nc", variable="mask", bands=8, mask_labels=False)
    if empty_group:
        xr.Dataset().to_netcdf(mask, group="sensor_band_parameters", mode="a", engine="h5netcdf")
    with read_cube(rfl, mask=mask) as cube:
        assert cube.aerosol_optical_depth[0, 0] == pytest.approx(0.05)
        assert cube.water_vapor[0, 0] == pytest.approx(0.06)
        assert cube.aerosol_optical_depth.attrs["alias_source"] == "ATBD channel order"
        assert cube.water_vapor.attrs["alias_source"] == "ATBD channel order"


@pytest.mark.parametrize("missing", [(5,), (6,), (5, 6)])
def test_v001_mask_aliases_reject_unrecognized_published_labels(tmp_path, missing):
    rfl = write_product(tmp_path / "rfl.nc")
    mask = write_product(tmp_path / "mask.nc", variable="mask", bands=8)
    labels = V001_MASK_LABELS.copy()
    for index in missing:
        labels[index] = "Unknown channel"
    xr.Dataset({"mask_bands": ("bands", labels)}).to_netcdf(
        mask, group="sensor_band_parameters", mode="a", engine="h5netcdf",
    )
    with pytest.raises(ValueError, match="V001 mask_bands labels must identify"):
        read_cube(rfl, mask=mask)
