"""Rasterise published SAFE detector polygons onto an existing native window."""

import re
import xml.etree.ElementTree as ET

import numpy as np
import xarray as xr


def read_gml_mask(path, *, shape, transform, crs):
    """Read MSK_DETFOO GML, including holes, without changing image support."""
    from rasterio.features import rasterize
    from rasterio.warp import transform_geom

    root = ET.parse(path).getroot()
    local = lambda el: el.tag.rsplit("}", 1)[-1]
    default_crs = next((el.attrib["srsName"] for el in root.iter() if "srsName" in el.attrib), crs)
    shapes = []
    for feature in root.iter():
        identifier = next((v for k, v in feature.attrib.items() if k.rsplit("}", 1)[-1] == "id"), "")
        match = re.search(r"-B(?:\d{2}|8A)-(\d+)(?:-|$)", identifier)
        if match is None:
            continue
        detector = int(match[1])
        if not 1 <= detector <= 12:
            raise ValueError("invalid MSI detector identifier")
        for polygon in feature.iter():
            if local(polygon) not in {"Polygon", "PolygonPatch"}:
                continue
            rings = []
            for role in ("exterior", "interior"):
                for ring in polygon.iter():
                    if local(ring) != role:
                        continue
                    pos = next((el for el in ring.iter() if local(el) == "posList"), None)
                    if pos is None or pos.text is None:
                        raise ValueError("detector polygon needs a posList")
                    dimensions = int(pos.attrib.get("srsDimension", polygon.attrib.get("srsDimension", 2)))
                    values = np.fromstring(pos.text, sep=" ")
                    if dimensions != 2 or len(values) < 8 or len(values) % 2:
                        raise ValueError("invalid detector polygon coordinates")
                    rings.append(values.reshape(-1, 2).tolist())
            if not rings:
                continue
            geometry = {"type": "Polygon", "coordinates": rings}
            source_crs = polygon.attrib.get("srsName", default_crs)
            geometry = transform_geom(source_crs, crs, geometry)
            shapes.append((geometry, detector))
    if not shapes:
        raise ValueError("no labelled detector polygons in GML")
    values = rasterize(shapes, out_shape=shape, transform=transform, fill=0, dtype="uint8")
    return xr.DataArray(values, dims=("y", "x"))
