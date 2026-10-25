"""EMIT L1B RAD+OBS in raw sensor geometry, with labelled Gaussian SRFs."""

from contextlib import ExitStack
import json
from pathlib import Path

import numpy as np
import xarray as xr

from spectraccess.core.assumptions import AssumptionBasis, AssumptionRecord
from spectraccess.core.imagery import describe, imagery, measurement_group, site_box, unknown
from .cube import _open, _science, _version, normalize_provider_label

SRF_ASSUMPTION = AssumptionRecord(
    "emit_gaussian_srf", "Gaussian shape from published centre and half-max FWHM, "
    "declared column-invariant; no per-column response is published.",
    AssumptionBasis.OPERATOR_DECLARED, "Tabulated per-column provider responses available.",
)
FOOTPRINT_ASSUMPTION = AssumptionRecord(
    "emit_raw_pixel_corners", "Raw-pixel corners interpolated from neighbouring geolocation "
    "centres, linearly extrapolated at swath edges. These are approximate footprints.",
    AssumptionBasis.OPERATOR_DECLARED, "Provider raw-pixel corner coordinates available.",
)


def _corners(values):
    """Average neighbouring centres; one-sided edge extrapolation, no GLT."""
    if min(values.shape) < 2:
        raise ValueError("at least two raw lines and columns required for footprint evaluation")
    v = np.pad(values, 1, mode="edge")
    v[0, 1:-1] = 2*values[0] - values[1]
    v[-1, 1:-1] = 2*values[-1] - values[-2]
    v[:, 0] = 2*v[:, 1] - v[:, 2]
    v[:, -1] = 2*v[:, -2] - v[:, -3]
    vertices = (v[:-1, :-1] + v[1:, :-1] + v[:-1, 1:] + v[1:, 1:]) / 4
    return np.stack([vertices[:-1, :-1], vertices[:-1, 1:], vertices[1:, 1:], vertices[1:, :-1]], axis=-1)


def read_imagery(radiance, *, observation=None, bounds=None,
                 solar_irradiance=None, solar_spectrum=None):
    """Open local L1B RAD and optional OBS; caller must close the DataTree.

    An optional band irradiance array and named spectrum permit a labelled
    reflectance conversion. Without them, reflectance is explicitly unknown.
    Site boxes use raw sensor-pixel footprints, never GLT cells. GLT indices
    remain provider one-based values with their original fill values.
    """
    stack = ExitStack()
    try:
        root = _open(stack, radiance)
        version = _version(dict(root.attrs))
        rad = root["radiance"]
        if rad.dims != ("downtrack", "crosstrack", "bands"):
            raise ValueError("expected raw EMIT RAD dimensions (downtrack,crosstrack,bands)")
        rename = {"downtrack": "line", "crosstrack": "column", "bands": "band"}
        rad = _science(rad, default_fill=-9999).rename(rename).transpose("band", "line", "column")
        rad = rad.chunk({"band": 16, "line": 128, "column": 128})
        if not rad.attrs.get("units"):
            raise ValueError("provider radiance units missing")
        params = _open(stack, radiance, "sensor_band_parameters")
        centre, fwhm = params["wavelengths"], params["fwhm"]
        if centre.size != rad.sizes["band"] or fwhm.shape != centre.shape:
            raise ValueError("spectral parameters do not match radiance channels")
        location = _open(stack, radiance, "location")
        geometry = {}
        for name, standard in (("lat", "latitude"), ("lon", "longitude")):
            arr = _science(location[name]).rename({k: v for k, v in rename.items() if k in location[name].dims})
            if arr.dims != ("line", "column") or arr.shape != rad.shape[1:]:
                raise ValueError("geolocation must match raw RAD pixels")
            geometry[standard] = describe(arr, basis="published", source=str(radiance))
        # Unwrap longitudes before interpolating across the date line.
        lon = np.degrees(np.unwrap(np.radians(geometry["longitude"].values), axis=1))
        for name, values in (("footprint_longitude", lon), ("footprint_latitude", geometry["latitude"].values)):
            geometry[name] = describe(xr.DataArray(_corners(values), dims=("line", "column", "vertex")),
                                      basis="evaluated_from_provider_model", source="EMIT location lat/lon",
                                      assumptions=[FOOTPRINT_ASSUMPTION], units="degree")
        if observation is not None:
            obs_root = _open(stack, observation)
            if _version(dict(obs_root.attrs)) != version:
                raise ValueError("RAD/OBS versions differ")
            for key in ("time_coverage_start", "orbit", "scene"):
                if key in root.attrs and key in obs_root.attrs and root.attrs[key] != obs_root.attrs[key]:
                    raise ValueError(f"RAD/OBS {key} differs")
            obs = _science(obs_root["obs"])
            if obs.shape[:2] != rad.shape[1:]:
                raise ValueError("OBS pixels do not match RAD")
            labels = _open(stack, observation, "sensor_band_parameters")["observation_bands"]
            names = [normalize_provider_label(v) for v in labels.values]
            if len(names) != obs.shape[2] or len(set(names)) != len(names):
                raise ValueError("invalid OBS labels")
            for label, standard in (("to_sensor_zenith", "view_zenith"), ("to_sensor_azimuth", "view_azimuth"),
                                     ("to_sun_zenith", "sun_zenith"), ("to_sun_azimuth", "sun_azimuth"),
                                     ("earth_sun_distance", "earth_sun_distance")):
                if label in names:
                    arr = obs.isel({obs.dims[2]: names.index(label)}, drop=True).rename({"downtrack": "line", "crosstrack": "column"})
                    geometry[standard] = describe(arr, basis="published", source=str(observation),
                                                  units="AU" if standard == "earth_sun_distance" else "degree")
            if "utc_time" in names and "time_coverage_start" in root.attrs:
                hours = obs.isel({obs.dims[2]: names.index("utc_time")}, drop=True).values
                # Provider labels specify mid-line time; retain its line dimensionality.
                mid = hours[:, hours.shape[1] // 2]
                unwrapped = np.unwrap(mid * np.pi / 12) * 12 / np.pi
                start = np.datetime64(str(root.attrs["time_coverage_start"]).removesuffix("Z"), "ns")
                midnight = start.astype("datetime64[D]")
                if np.isfinite(unwrapped[0]):
                    start_seconds = float((start - midnight) / np.timedelta64(1, "s"))
                    unwrapped += 24 * round((start_seconds - unwrapped[0]*3600) / 86400)
                seconds = (midnight - np.datetime64("1970-01-01", "D")) / np.timedelta64(1, "s") + unwrapped * 3600
                geometry["time"] = describe(xr.DataArray(seconds, dims="line"), basis="published", source=str(observation),
                    units="seconds since 1970-01-01 00:00:00 UTC", provider_definition="UTC decimal hours for mid-line pixels")
        for name in ("time", "view_zenith", "view_azimuth", "sun_zenith", "sun_azimuth"):
            if name not in geometry:
                geometry[name] = unknown("required OBS field or companion absent")
        conversion = {"solar_spectrum": solar_spectrum or "unknown",
                      "sun_angle_convention": "per raw pixel OBS sun zenith, no clipping",
                      "earth_sun_distance": "OBS Earth-sun distance (AU)"}
        if solar_irradiance is not None:
            if not solar_spectrum:
                raise ValueError("solar irradiance requires a named spectrum")
            e0 = xr.DataArray(np.asarray(solar_irradiance), dims="band")
            if e0.size != rad.sizes["band"] or not np.all(e0.values > 0):
                raise ValueError("solar irradiance must be positive and match bands")
            if "earth_sun_distance" not in geometry:
                raise ValueError("reflectance conversion requires OBS Earth-sun distance")
            rho = np.pi * rad * geometry["earth_sun_distance"]**2 / (e0 * np.cos(np.radians(geometry["sun_zenith"])))
            conversion["solar_irradiance"] = e0.values.tolist()
            assumption = AssumptionRecord("emit_reflectance_spectrum", "Caller supplied solar spectrum " + solar_spectrum,
                AssumptionBasis.OPERATOR_DECLARED, "Provider specifies the solar conversion spectrum.")
            rho = describe(rho, basis="evaluated_from_provider_model", units="1", assumptions=[assumption],
                           conversion=json.dumps(conversion))
        else:
            rho = describe(xr.full_like(rad, np.nan), basis="unknown", units="1",
                           reason="no solar irradiance spectrum supplied", conversion=json.dumps(conversion))
        rho.attrs["sun_normalisation"] = "true_rho_without_clip"
        group = measurement_group({
            "radiance": describe(rad, basis="published", source=str(radiance), energy_photon_basis="energy"),
            "toa_reflectance": rho,
        }, primary="radiance", geometry_kind="sensor_geometry_with_lookup",
           provider_level="L1B", sensor="EMIT", product_version=version,
           processing_chain=[{"kind": "calibration", "actor": "NASA/JPL", "processor": "EMIT L1B",
                              "parameters": {"product_version": version, "source": "JPL D-107862 v1.0"}}])
        group.update(geometry)
        # GLT remains on its own ortho grid. It is not imagery and is not cropped
        # with raw lines; it continues to reference original one-based indices.
        glt = xr.Dataset({name: describe(arr, basis="published", source=str(radiance))
                          for name, arr in location.data_vars.items() if name.startswith("glt_")},
                         attrs={k: v for k, v in location.attrs.items()})
        band = np.arange(rad.sizes["band"])
        srfs = xr.Dataset({
            "centre": ("band", centre.values, dict(centre.attrs, basis="published", uncertainty_status="unknown")),
            "fwhm": ("band", fwhm.values, dict(fwhm.attrs, basis="published", uncertainty_status="unknown")),
        }, coords={"band": band}, attrs={
            "representation": "parametric", "shape": "Gaussian", "shape_basis": "operator_declared",
            "flight_unit": "EMIT on ISS",
            "width_definition": "half_max_fwhm", "source": str(radiance), "source_version": version,
            "column_dependence": "declared invariant", "energy_photon_basis": "energy",
            "assumptions": json.dumps([SRF_ASSUMPTION.to_dict()]),
        })
        # Original indices survive cropping for GLT and keyed SRF lookup.
        group = group.assign_coords(band=band, line=np.arange(rad.sizes["line"]), column=np.arange(rad.sizes["column"]))
        tree = imagery({"swath": group}, spectral_responses={"emit": srfs}, sensor="EMIT", product_id=Path(radiance).name)
        tree["/geolocation_lookup"] = xr.DataTree(glt)
        if bounds is not None:
            tree = site_box(tree, bounds)
        tree.set_close(stack.close)
        return tree
    except BaseException:
        stack.close()
        raise
