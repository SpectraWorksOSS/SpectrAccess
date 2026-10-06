"""Read provider EMIT groups in raw instrument geometry."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

import xarray as xr

if TYPE_CHECKING:
    from .connector import EMITTarget


def _open(stack: ExitStack, path: str | Path, group: str | None = None) -> xr.Dataset:
    # Preserve integer GLT fill and flag values; decode science fill explicitly.
    return stack.enter_context(xr.open_dataset(
        path, group=group, engine="h5netcdf", mask_and_scale=False,
        decode_times=False,
    ))


def _version(attrs: dict) -> str:
    value = attrs.get("product_version", attrs.get("version"))
    value = str(value).upper().removeprefix("V").zfill(3)
    if value not in {"001", "002"}:
        raise ValueError(f"missing or unsupported EMIT product version: {value!r}")
    return value


def read_product_metadata(path: str | Path, *, target: EMITTarget | None = None) -> dict:
    """Read file metadata using the same version checks as the cube reader."""
    with ExitStack() as stack:
        attrs = dict(_open(stack, path).attrs)
        version = _version(attrs)
        if target is not None and version != target.version:
            raise ValueError(f"file version {version} differs from CMR version {target.version}")
        return attrs


def _science(array: xr.DataArray, *, default_fill: float | None = None) -> xr.DataArray:
    fill = array.attrs.get("_FillValue", default_fill)
    array = array.copy(deep=False)
    if fill is not None:
        array.attrs["_FillValue"] = fill
    # CF's backend wrapper decodes fill lazily, avoiding an eager full-cube
    # value comparison. It also honors provider scale_factor/add_offset.
    return xr.decode_cf(array.to_dataset(), decode_times=False)[array.name]


def read_cube(
    reflectance: str | Path,
    *,
    uncertainty: str | Path | None = None,
    mask: str | Path | None = None,
    observation: str | Path | None = None,
    target: EMITTarget | None = None,
) -> xr.Dataset:
    """Return a lazy raw EMIT L2A dataset; caller must call ``close()``.

    Optional inputs are explicit local paths. Missing companions are omitted.
    No orthorectification, band filtering, or spectral transfer is performed.
    """
    stack = ExitStack()
    try:
        attrs = read_product_metadata(reflectance, target=target)
        version = _version(attrs)
        root = _open(stack, reflectance)
        rfl = root["reflectance"]
        raw_dims = ("downtrack", "crosstrack", "bands")
        if rfl.dims != raw_dims:
            raise ValueError(f"reflectance dimensions must be {raw_dims}, got {rfl.dims}")
        result = xr.Dataset({"reflectance": _science(rfl, default_fill=-9999)}, attrs=attrs)
        params = _open(stack, reflectance, "sensor_band_parameters")
        for name in ("wavelengths", "fwhm", "good_wavelengths"):
            param = params[name]
            if param.shape != (rfl.sizes["bands"],):
                raise ValueError(f"{name} does not match reflectance bands")
            param = param.rename({param.dims[0]: "bands"})
            xr.align(rfl, param, join="exact")
            result[name] = param
        location = _open(stack, reflectance, "location")
        for name, array in location.data_vars.items():
            if name in {"lat", "lon", "elev"} and (
                array.dims != raw_dims[:2] or array.shape != rfl.shape[:2]
            ):
                raise ValueError(f"{name} does not match raw reflectance pixels")
            xr.align(rfl, array, join="exact")
            result[name] = array  # Includes GLT values and their published fill.
        result = result.set_coords([name for name in ("lat", "lon", "wavelengths") if name in result])

        for path, variable, band_dim in (
            (uncertainty, "reflectance_uncertainty", "bands"),
            (mask, "mask", "mask_bands"),
            (observation, "obs", "observation_bands"),
        ):
            if path is None:
                continue
            companion = _open(stack, path)
            if _version(dict(companion.attrs)) != version:
                raise ValueError(f"{variable} version differs from reflectance")
            # Compare provider scene identifiers when present, without guessing
            # from filenames (OBS may have a different L1B product name).
            for key in ("time_coverage_start", "orbit", "scene"):
                if key in attrs and key in companion.attrs and attrs[key] != companion.attrs[key]:
                    raise ValueError(f"{variable} {key} differs from reflectance")
            array = companion[variable]
            if array.dims[:2] != raw_dims[:2] or array.shape[:2] != rfl.shape[:2]:
                raise ValueError(f"{variable} does not match raw reflectance pixels")
            if array.ndim != 3:
                raise ValueError(f"{variable} must have three dimensions")
            if variable == "reflectance_uncertainty" and array.shape != rfl.shape:
                raise ValueError("uncertainty does not match reflectance bands")
            array = array.rename({array.dims[2]: band_dim})
            xr.align(rfl, array, join="exact", exclude={band_dim} if band_dim != "bands" else set())
            result[variable] = _science(
                array, default_fill=-9999 if variable == "reflectance_uncertainty" else None,
            )
            if variable == "reflectance_uncertainty":
                # NASA EMIT L2A ATBD, section 5: provider definition only.
                result[variable].attrs["provider_definition"] = (
                    "predicted uncertainty in the reflectance measurement for each channel, "
                    "in units of standard deviations (presuming a Gaussian distribution)."
                )
                result[variable].attrs.setdefault("long_name", "Reflectance uncertainty (one standard deviation)")
                result["reflectance"].attrs["ancillary_variables"] = variable
            else:
                labels = _open(stack, path, "sensor_band_parameters")
                for name, param in labels.data_vars.items():
                    if param.ndim == 1 and param.size == array.shape[2]:
                        result[f"{variable}_{name}"] = param.rename({param.dims[0]: band_dim})
                if variable == "mask" and version == "001" and array.shape[2] >= 8:
                    # V001 provider MASK layout, zero-based bands 6 and 7.
                    result["aerosol_optical_depth"] = result["mask"].isel(mask_bands=6, drop=True)
                    result["water_vapor"] = result["mask"].isel(mask_bands=7, drop=True)
                # V002 standalone MASK may publish additional value arrays.
                for name, value in companion.data_vars.items():
                    if name == variable:
                        continue
                    if name in result:
                        raise ValueError(f"duplicate companion variable {name}")
                    for dim in raw_dims[:2]:
                        if dim in value.sizes and value.sizes[dim] != rfl.sizes[dim]:
                            raise ValueError(f"{name} does not match raw reflectance pixels")
                    result[name] = _science(value)
        result.set_close(stack.close)
        return result
    except BaseException:
        stack.close()
        raise
