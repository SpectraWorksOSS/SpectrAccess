"""Optional rasterio input, retaining native pixel corners and CRS."""

import json

import numpy as np
import xarray as xr

from .model import describe


def read_band(path, *, bounds=None):
    """Read one local band, optionally only a whole-pixel geographic window."""
    import rasterio
    from rasterio.warp import transform_bounds, transform
    from rasterio.windows import Window, from_bounds

    with rasterio.open(path) as src:
        if src.crs is None:
            raise ValueError(f"raster lacks CRS: {path}")
        window = Window(0, 0, src.width, src.height)
        if bounds is not None:
            projected = transform_bounds("EPSG:4326", src.crs, *bounds, densify_pts=21)
            raw = from_bounds(*projected, transform=src.transform)
            left, top = np.floor([raw.col_off, raw.row_off]).astype(int)
            right, bottom = np.ceil([raw.col_off + raw.width, raw.row_off + raw.height]).astype(int)
            if right <= 0 or bottom <= 0 or left >= src.width or top >= src.height:
                raise ValueError("site box does not intersect raster")
            window = Window(max(0, left), max(0, top),
                            min(src.width, right) - max(0, left),
                            min(src.height, bottom) - max(0, top))
        value = src.read(1, window=window, masked=True).astype(float).filled(np.nan)
        t = src.window_transform(window)
        rows, cols = np.indices(value.shape)
        centre_x, centre_y = t * (cols + .5, rows + .5)
        lon, lat = transform(src.crs, "EPSG:4326", centre_x.ravel(), centre_y.ravel())
        data = xr.Dataset({"dn": (("y", "x"), value)}, coords={
            "y": np.arange(value.shape[0]) + int(window.row_off),
            "x": np.arange(value.shape[1]) + int(window.col_off),
        }, attrs={"crs": src.crs.to_string(), "transform": json.dumps(list(t)[:6]),
                  "source": str(path)})
        for name, values, units in (("longitude", lon, "degrees_east"),
                                    ("latitude", lat, "degrees_north")):
            data[name] = describe(xr.DataArray(np.asarray(values).reshape(value.shape), dims=("y", "x")),
                                  basis="evaluated_from_provider_model", source=str(path), units=units)
        corners = []
        for dc, dr in ((0, 0), (1, 0), (1, 1), (0, 1)):
            cx, cy = t * (cols + dc, rows + dr)
            lo, la = transform(src.crs, "EPSG:4326", cx.ravel(), cy.ravel())
            corners.append((np.asarray(lo).reshape(value.shape), np.asarray(la).reshape(value.shape)))
        for i, name in enumerate(("footprint_longitude", "footprint_latitude")):
            data[name] = describe(xr.DataArray(np.stack([c[i] for c in corners], axis=-1),
                                               dims=("y", "x", "vertex")),
                                  basis="evaluated_from_provider_model", source=str(path),
                                  units="degrees_east" if i == 0 else "degrees_north")
        return data
