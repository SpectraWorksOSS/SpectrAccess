"""Read published SRF tables without normalising, fitting or interpolating."""

import json
import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import xarray as xr

ESA_SRF_URL = 'https://sentiwiki.copernicus.eu/__attachments/a_ece6183b6698587e7ecd9804974bece3d82e39e151aa5aeca9d8fd95e1f1558c/COPE-GSEG-EOPG-TN-15-0007%20-%20Sentinel-2%20Spectral%20Response%20Functions%202024%20-%204.0.xlsx'
ESA_SRF_SHA256 = '1a9edc27d692a570911a460d589f188da0fc3e27f0b0bd1ad322059c380519b0'

USGS_RSR = {
    "LANDSAT_8": ("L8_OLI_RSR.xlsx", "v1.2", "6b8bb8bf0c6f1a3cef56eb497096283f6cd66f1af163989eb03c59aa6588cfe0"),
    "LANDSAT_9": ("L9_OLI2_RSR.xlsx", "v2-1", "31b6c3c618716419d2c2a650b77ae8c4604abde36af84c7bd38031e58c174812"),
}


def read_usgs_srf(*, unit):
    """Read bundled, pinned USGS band-average in-band RSRs at native sampling.

    Curves and their published spread retain the workbook samples, including
    small negative measurement values. No common wavelength grid is imposed.
    Missing tail entries in the rectangular table are NaN, not zero response.
    """
    if unit not in USGS_RSR:
        raise ValueError("USGS RSR unit must be LANDSAT_8 or LANDSAT_9")
    filename, version, digest = USGS_RSR[unit]
    path = Path(__file__).with_name("data") / filename
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("bundled USGS RSR checksum differs from its provenance pin")
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    relns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    names = ["CoastalAerosol", "Blue", "Green", "Red", "NIR", "SWIR1", "SWIR2", "Pan", "Cirrus"]
    profiles = []
    with zipfile.ZipFile(path) as workbook:
        sheets = ET.fromstring(workbook.read("xl/workbook.xml"))
        relationships = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        targets = {el.attrib["Id"]: el.attrib["Target"].lstrip("/") for el in relationships}
        sheet_paths = {el.attrib["name"]: targets[el.attrib[f"{{{relns}}}id"]]
                       for el in sheets.find("m:sheets", ns)}
        for name in names:
            target = sheet_paths[name]
            if not target.startswith("xl/"):
                target = "xl/" + target
            root = ET.fromstring(workbook.read(target))
            rows = []
            for row in root.findall("m:sheetData/m:row", ns)[1:]:
                cells = {''.join(c for c in cell.attrib['r'] if c.isalpha()): cell for cell in row}
                values = []
                for column in ("A", "B", "C"):
                    cell = cells.get(column)
                    value = cell.find("m:v", ns) if cell is not None else None
                    values.append(float(value.text) if value is not None and cell.get("t") != "s" else np.nan)
                if np.isfinite(values[:2]).all():
                    rows.append(values)
            table = np.asarray(rows)
            if not len(rows) or not np.all(np.diff(table[:, 0]) > 0):
                raise ValueError("invalid bundled USGS wavelength samples")
            profiles.append(table)
    length = max(len(p) for p in profiles)
    data = np.full((9, length, 3), np.nan)
    for index, profile in enumerate(profiles):
        data[index, :len(profile)] = profile
    ds = xr.Dataset({name: (("band", "wavelength_sample"), data[:, :, i],
                            {"basis": "published", "uncertainty_status": "unknown", "units": "nm" if i == 0 else "1"})
                     for i, name in enumerate(("wavelength", "response", "stddev"))},
                    coords={"band": [f"B{i}" for i in range(1, 10)]}, attrs={
                        "representation": "tabulated", "flight_unit": unit, "source_version": version,
                        "source": "https://landsat.usgs.gov/landsat/spectral_viewer/bands/" + filename,
                        "source_sha256": digest, "normalisation": "as published", "range": "in-band",
                    })
    ds["stddev"].attrs["definition"] = "published workbook spread column; not promoted to measurement uncertainty"
    if unit == "LANDSAT_9":
        ds.attrs["energy_photon_basis"] = "energy (workbook BA RSR [watts])"
    return ds


def read_esa_srf(path, *, unit, version="4.0"):
    """Read the ESA v4.0 workbook as a separate, versioned SRF entry.

    Requires a local provider workbook, not a network request. The embedded
    SAFE entries keep their own product identity and are never overwritten.
    """
    if version != "4.0" or unit not in {"S2A", "S2B", "S2C"}:
        raise ValueError("this workbook reader supports ESA v4.0, S2A/S2B/S2C")
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != ESA_SRF_SHA256:
        raise ValueError("ESA v4.0 SRF checksum differs from its provenance pin")
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    sheet = {"S2A": 2, "S2B": 3, "S2C": 4}[unit]
    with zipfile.ZipFile(path) as workbook:
        strings = ["".join(el.itertext()) for el in ET.fromstring(workbook.read("xl/sharedStrings.xml"))]
        root = ET.fromstring(workbook.read(f"xl/worksheets/sheet{sheet}.xml"))
        rows = root.findall("m:sheetData/m:row", ns)
        headers = [strings[int(c.find("m:v", ns).text)] for c in list(rows[0])[:14]]
        expected = [f"{unit}_SR_AV_B{b}" for b in (1, 2, 3, 4, 5, 6, 7, 8, "8A", 9, 10, 11, 12)]
        if headers != ["SR_WL"] + expected:
            raise ValueError("unexpected SRF workbook columns; do not relabel an unknown format")
        data = []
        for row in rows[1:]:
            values = [c.find("m:v", ns) for c in list(row)[:14]]
            if len(values) == 14 and all(v is not None for v in values):
                data.append([float(v.text) for v in values])
    table = np.asarray(data)
    if table.ndim != 2 or not np.all(np.diff(table[:, 0]) > 0):
        raise ValueError("invalid SRF wavelength grid")
    bands = ["B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B10", "B11", "B12"]
    return xr.Dataset({
        "wavelength": ("wavelength_sample", table[:, 0], {"units": "nm", "basis": "published", "uncertainty_status": "unknown"}),
        "response": (("band", "wavelength_sample"), table[:, 1:].T,
                     {"units": "1", "basis": "published", "uncertainty_status": "unknown"}),
    }, coords={"band": bands}, attrs={
        "representation": "tabulated", "flight_unit": unit, "source_version": version,
        "source": str(Path(path)), "source_sha256": ESA_SRF_SHA256, "source_url": ESA_SRF_URL, "identity": json.dumps(["ESA S2-SRF", version, unit]),
        "normalisation": "as published", "source_document": "COPE-GSEG-EOPG-TN-15-0007",
    })


def read_tabulated_srf(path, *, source, version, unit):
    """Read a long CSV table with band,wavelength,response (optional stddev).

    Wavelength unit is nm. Provider flight-unit and version are explicit.
    This format can carry the USGS/GSFC band-average OLI RSR, including its
    published spread column, without resampling or peak normalisation.
    """
    import pandas as pd
    frame = pd.read_csv(path, dtype={"band": str})
    if not {"band", "wavelength", "response"} <= set(frame):
        raise ValueError("SRF CSV needs band,wavelength,response")
    if frame.duplicated(["band", "wavelength"]).any():
        raise ValueError("duplicate SRF band/wavelength keys")
    ds = frame.set_index(["band", "wavelength"]).to_xarray().rename({"wavelength": "wavelength_sample"})
    ds["wavelength"] = ds.wavelength_sample.copy()
    ds["wavelength"].attrs["units"] = "nm"
    ds["response"].attrs["units"] = "1"
    for arr in ds.data_vars.values():
        arr.attrs.update(basis="published", uncertainty_status="unknown")
    ds.attrs.update(representation="tabulated", source=source, source_version=version,
                    flight_unit=unit, normalisation="as published")
    return ds
