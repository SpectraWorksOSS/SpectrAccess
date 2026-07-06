from __future__ import annotations

import os
import sys

from spectraccess.connectors.gsics.connector import DEFAULT_CATALOGS, GSICSCatalog, GSICSConnector
from spectraccess.connectors.modis_viirs_cal.connector import VIIRSCatalog, VIIRSCalibrationConnector
from spectraccess.connectors.radcalnet import RadCalNetConnector


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
    if not catalog_url:
        # The VIIRS connector is a documented stub until the NOAA STAR
        # THREDDS backend is reachable again (down as of 2026-07-05) and a
        # verified catalog URL is configured. A known-not-live connector must
        # not raise here: on the weekly schedule that would file a recurring
        # false connector-broken issue every run. Skip cleanly instead.
        print("VIIRS smoke SKIPPED: SPECTRACCESS_VIIRS_CATALOG_URL not configured (connector is a documented stub)")
        return
    targets = VIIRSCalibrationConnector(VIIRSCatalog("NOAA STAR VIIRS F-factors", catalog_url)).discover(
        use_cache=False,
        timeout=20,
    )
    if not targets:
        raise RuntimeError("VIIRS discover returned no targets")


def smoke_radcalnet() -> None:
    # RadCalNet requires an approved account; weekly CI has no credentials.
    # An unset env must SKIP cleanly, not fail -- same pattern as VIIRS above.
    if not os.environ.get("RADCALNET_USERNAME") or not os.environ.get("RADCALNET_PASSWORD"):
        print("RadCalNet smoke SKIPPED: RADCALNET_USERNAME/RADCALNET_PASSWORD not set")
        return

    connector = RadCalNetConnector()
    sites = connector.sites()
    if len(sites) < 6:
        raise RuntimeError(f"RadCalNet sites() returned too few sites: {sites}")
    print(f"RadCalNet sites: found {len(sites)} site(s)")

    targets = connector.discover(site=sites[0], kind="output")
    if not targets:
        # Some sites may have no .output files yet; fall back to scanning
        # every site for the first one that does.
        for site in sites:
            targets = connector.discover(site=site, kind="output")
            if targets:
                break
    if not targets:
        raise RuntimeError("RadCalNet discover returned no .output targets for any site")

    target = targets[-1]  # sorted by (site, year, doy) -- last is newest
    print(f"RadCalNet fetch: {target.site}/{target.filename}")
    raw = connector.fetch(target)

    df = connector.parse(raw)
    if df.empty:
        raise RuntimeError("RadCalNet parse produced an empty DataFrame")

    canonical = connector.parse_canonical(raw, source_url=target.url)
    if canonical.empty:
        raise RuntimeError("RadCalNet parse_canonical produced an empty DataFrame")
    if canonical.attrs.get("spectraccess_schema_version") is None:
        raise RuntimeError("RadCalNet canonical frame is not schema-stamped")
    # Whether a given day's uncertainties are measured ("provided") or
    # climatological ("prior", the R2 spec's negative-value flag) depends on
    # the file -- e.g. BSCN00_2026_182 carries only climatological ones. The
    # smoke asserts the uncertainty RECORD is populated (any non-"unknown"
    # status), not which provenance the site happened to publish that day.
    if not (canonical["unc_status"] != "unknown").any():
        raise RuntimeError("RadCalNet canonical frame has only 'unknown' uncertainty rows")

    statuses = canonical["unc_status"].value_counts().to_dict()
    print(f"RadCalNet parse_canonical: shape={canonical.shape}, unc_status={statuses}")


def main() -> int:
    connector = sys.argv[1] if len(sys.argv) > 1 else ""
    if connector == "gsics":
        smoke_gsics()
    elif connector == "modis_viirs_cal":
        smoke_viirs()
    elif connector == "radcalnet":
        smoke_radcalnet()
    else:
        raise SystemExit(f"unknown connector {connector!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
