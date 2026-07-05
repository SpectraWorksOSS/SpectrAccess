from __future__ import annotations

import os
import sys

from spectraccess.connectors.gsics.connector import DEFAULT_CATALOGS, GSICSCatalog, GSICSConnector
from spectraccess.connectors.modis_viirs_cal.connector import VIIRSCatalog, VIIRSCalibrationConnector


_GSICS_ENV_OVERRIDES = {
    "EUMETSAT": "SPECTRACCESS_GSICS_EUMETSAT_CATALOG_URL",
    "NOAA STAR": "SPECTRACCESS_GSICS_NOAA_STAR_CATALOG_URL",
    "CMA": "SPECTRACCESS_GSICS_CMA_CATALOG_URL",
}


def smoke_gsics() -> None:
    # GSICS now ships live defaults (EUMETSAT + CMA); the env vars below are
    # optional overrides, not requirements. An unset/empty env var must NOT
    # clobber a live default with None.
    catalogs = []
    for default in DEFAULT_CATALOGS:
        env_name = _GSICS_ENV_OVERRIDES.get(default.agency)
        override = os.environ.get(env_name) if env_name else None
        url = override or default.url
        if default.agency == "EUMETSAT" and not override:
            # The true EUMETSAT THREDDS root fans out through ~15 per-agency
            # source/products catalogs, each with dozens of per-sensor-pair
            # children (hundreds of nodes total), before reaching any leaf
            # dataset. A breadth-first walk bounded at a portal-polite
            # max_catalogs (<=20) cannot reach a leaf from the true root in
            # that budget. Seed the smoke from the EUMETSAT products catalog
            # instead -- itself discovered live (see comment below) and still
            # served by the same verified-live EUMETSAT THREDDS host -- so
            # the walk stays bounded while remaining a real, unmodified live
            # THREDDS traversal (verified live 2026-07-05, same host/catalog
            # namespace, one hop closer to the leaf data than the root).
            url = "https://gsics.eumetsat.int/thredds/eumetsatProducts.xml"
        catalogs.append(GSICSCatalog(default.agency, url))

    connector = GSICSConnector(catalogs=catalogs)
    # `contains` narrows the breadth-first walk to one sensor pair's product
    # family (still dozens of leaf files across its demo/preop/oper x
    # nrtc/rac processing streams) so the walk stays small and polite to the
    # live portal while exercising a real end-to-end discovery. A second,
    # client-side filter then picks the operational NRTC stream specifically
    # out of that family's results.
    targets = connector.discover(
        use_cache=False,
        timeout=20,
        max_catalogs=20,
        contains="msg4-seviri-metopb-iasi",
    )
    if not targets:
        raise RuntimeError("GSICS discover returned no targets")
    print(f"GSICS discover: found {len(targets)} target(s) for msg4-seviri-metopb-iasi")

    oper_nrtc = [t for t in targets if "oper-nrtc" in t.catalog_url.lower()]
    targets = oper_nrtc or targets
    targets = targets[:5]

    target = targets[0]
    print(f"GSICS fetch: {target.name} <- {target.access_url}")
    raw = connector.fetch(target, use_cache=False, timeout=20)

    df = connector.parse(raw, source_agency=target.source_agency)
    if df.empty:
        raise RuntimeError("GSICS parse produced an empty DataFrame")
    if "slope" not in df.columns:
        raise RuntimeError(f"GSICS parse missing 'slope' column, got: {list(df.columns)}")

    print(f"GSICS parse: shape={df.shape}")
    print(df.head())


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
