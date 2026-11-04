"""Sourced L1C processing history. Unlisted combinations remain unstated.

Source: https://sentiwiki.copernicus.eu/web/s2-processing , Collection-1
Processing Baseline table and PB 04.00 description (read 2026-10-25).
Collection identity cannot be recovered from Nxxxx in a SAFE name alone.
"""

SOURCE = "https://sentiwiki.copernicus.eu/web/s2-processing"
PROCESSING_HISTORY = {
    ("operational", "04.00"): {
        "harmonisation": "S2B radiometry aligned to S2A", "radiometric_offset": True,
        "in_operations_since": "2022-01-25",
    },
    ("collection-1", "05.00"): {
        "harmonisation": "S2B radiometry aligned to S2A", "radiometric_offset": True,
        "geometric_refining": "Copernicus DEM 30 m",
        "acquisition_dates": ["2015-07-04", "2021-12-31"],
    },
    ("collection-1", "05.10"): {
        "harmonisation": "S2B radiometry aligned to S2A", "radiometric_offset": True,
        "geometric_refining": "Copernicus DEM 30 m", "computing_optimisation": True,
        "acquisition_dates": ["2022-01-01", "2023-12-13"],
    },
    ("operational", "05.10"): {
        "harmonisation": "S2B radiometry aligned to S2A", "radiometric_offset": True,
        "geometric_refining": "Copernicus DEM 30 m", "computing_optimisation": True,
        "in_operations_since": "2023-12-13",
    },
}


def processing_steps(baseline, collection):
    """No extrapolation of a table row to unsourced baselines/collections."""
    row = PROCESSING_HISTORY.get((collection, baseline))
    if row is None:
        return []
    return [{"kind": "harmonisation", "actor": "ESA", "processor": "MSI L1C",
             "parameters": dict(row, source=SOURCE, processing_baseline=baseline, collection=collection)}]
