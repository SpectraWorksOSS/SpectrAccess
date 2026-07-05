from __future__ import annotations

import os
import sys

from spectraccess.connectors.gsics.connector import GSICSCatalog, GSICSConnector
from spectraccess.connectors.modis_viirs_cal.connector import VIIRSCatalog, VIIRSCalibrationConnector


def smoke_gsics() -> None:
    catalogs = [
        GSICSCatalog("EUMETSAT", os.environ.get("SPECTRACCESS_GSICS_EUMETSAT_CATALOG_URL")),
        GSICSCatalog("NOAA STAR", os.environ.get("SPECTRACCESS_GSICS_NOAA_STAR_CATALOG_URL")),
        GSICSCatalog("CMA", os.environ.get("SPECTRACCESS_GSICS_CMA_CATALOG_URL")),
    ]
    targets = GSICSConnector(catalogs=catalogs).discover(use_cache=False, timeout=20)
    if not targets:
        raise RuntimeError("GSICS discover returned no targets")


def smoke_viirs() -> None:
    catalog_url = os.environ.get("SPECTRACCESS_VIIRS_CATALOG_URL")
    targets = VIIRSCalibrationConnector(VIIRSCatalog("NOAA STAR VIIRS F-factors", catalog_url)).discover(
        use_cache=False,
        timeout=20,
    )
    if not targets:
        raise RuntimeError("VIIRS discover returned no targets")


def main() -> int:
    connector = sys.argv[1] if len(sys.argv) > 1 else ""
    if connector == "gsics":
        smoke_gsics()
    elif connector == "modis_viirs_cal":
        smoke_viirs()
    else:
        raise SystemExit(f"unknown connector {connector!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
