from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone

import requests

from spectraccess.connectors.gsics.connector import DEFAULT_CATALOGS, GSICSCatalog, GSICSConnector
from spectraccess.connectors.modis_viirs_cal.connector import VIIRSCatalog, VIIRSCalibrationConnector
from spectraccess.connectors.radcalnet import RadCalNetConnector
from spectraccess.connectors.sentinel2_cdse import Sentinel2CDSEConnector, target_to_canonical


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
        _probe_star_thredds()
        return
    targets = VIIRSCalibrationConnector(VIIRSCatalog("NOAA STAR VIIRS F-factors", catalog_url)).discover(
        use_cache=False,
        timeout=20,
    )
    if not targets:
        raise RuntimeError("VIIRS discover returned no targets")


STAR_THREDDS_URL = "https://www.star.nesdis.noaa.gov/thredds/gsics/catalog.xml"


def _probe_star_thredds() -> None:
    # The skip above must not hide the day NOAA STAR's THREDDS comes back.
    # One cheap GET of the canonical catalog: "reachable" only on HTTP 200
    # with a THREDDS catalog body (a 200 maintenance page is not the catalog).
    # Never raises: the result is printed and handed to the workflow through
    # GITHUB_OUTPUT (it opens an `upstream-back` issue), never turned into a
    # smoke failure.
    try:
        response = requests.get(STAR_THREDDS_URL, timeout=20)
        reachable = response.status_code == 200 and b"<catalog" in response.content[:4096]
        detail = f"HTTP {response.status_code}"
    except requests.RequestException as exc:
        reachable = False
        detail = type(exc).__name__
    state = "reachable" if reachable else "unreachable"
    print(f"NOAA STAR THREDDS probe: {state} ({detail}) <- {STAR_THREDDS_URL}")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as stream:
            stream.write(f"star_thredds={state}\n")


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


def smoke_sentinel2_cdse() -> None:
    # Catalogue discovery is public. Keep the scheduled smoke metadata-only:
    # downloading this ~757 MB fixture would need credentials and would turn a
    # portal-health check into recurring provider spend/transfer.
    connector = Sentinel2CDSEConnector(max_attempts=2)
    targets = connector.discover(
        mgrs_tile="31UFT",
        start=datetime(2024, 5, 1, tzinfo=timezone.utc),
        end=datetime(2024, 5, 1, tzinfo=timezone.utc),
        max_cloud_cover=20,
        limit=5,
    )
    fixture_id = "d085d39b-03e2-486d-ae2a-0c8deca9bdc0"
    matching = [target for target in targets if target.product_id == fixture_id]
    if not matching:
        raise RuntimeError(
            f"Sentinel-2 CDSE discovery did not return pinned fixture {fixture_id}; "
            f"got {[target.product_id for target in targets]}"
        )
    canonical = target_to_canonical(matching[0])
    if canonical.attrs.get("spectraccess_schema_version") is None:
        raise RuntimeError("Sentinel-2 CDSE canonical frame is not schema-stamped")
    if canonical.loc[0, "unc_status"] != "unknown":
        raise RuntimeError("Sentinel-2 CDSE cloud-cover uncertainty was not labelled unknown")
    print(
        "Sentinel-2 CDSE discovery: "
        f"{matching[0].title}, cloud_cover={matching[0].cloud_cover:.3f}%"
    )


def smoke_aeronet() -> None:
    # The AERONET v3 web service is public. One long-running site (NASA GSFC),
    # three days of L2.0: a ~100 kB CSV.
    from spectraccess.connectors.aeronet import AeronetConnector

    connector = AeronetConnector(timeout=60)
    target = connector.discover(
        "GSFC",
        datetime(2024, 6, 1, tzinfo=timezone.utc),
        datetime(2024, 6, 3, tzinfo=timezone.utc),
        level="L2.0",
    )[0]
    print(f"AERONET fetch: {target.url}")
    raw = connector.fetch(target)

    df = connector.parse(raw, requested_site=target.site, data_level=target.level)
    if df.empty:
        raise RuntimeError("AERONET parse produced an empty DataFrame")

    canonical = connector.parse_canonical(
        raw, source_url=target.url, requested_site=target.site, data_level=target.level
    )
    if canonical.empty:
        raise RuntimeError("AERONET parse_canonical produced an empty DataFrame")
    if canonical.attrs.get("spectraccess_schema_version") is None:
        raise RuntimeError("AERONET canonical frame is not schema-stamped")
    quantities = set(canonical["quantity"])
    if not {"aerosol_optical_depth", "precipitable_water"} <= quantities:
        raise RuntimeError(f"AERONET canonical frame missing AOD/PW quantities, got {sorted(quantities)}")
    # to_canonical deliberately asserts no per-observation uncertainty in v1.
    if not (canonical["unc_status"] == "unknown").all():
        raise RuntimeError("AERONET canonical uncertainty was not labelled unknown")
    print(
        f"AERONET parse: native={df.shape}, canonical={canonical.shape}, "
        f"observations={df['observation_index'].nunique()}"
    )


# A date for which the public NCEO ARD JASMIN mirror publishes the full SIAC
# GeoTIFF family (verified live 2026-09-25). Date directories after
# 2025-10-03 exist on the mirror but were empty at that check.
_CAMS_JASMIN_DATE = datetime(2025, 6, 1, tzinfo=timezone.utc)


def smoke_cams() -> None:
    from spectraccess.connectors.cams import JASMIN_BASE_URL, CAMSConnector

    # JASMIN is public, but one date's family is ~96 MB of GeoTIFFs: too much
    # for a weekly portal-health check. Exercise the connector's own
    # availability probe (HEAD on each of the three assets fetch() downloads)
    # instead of the transfer.
    connector = CAMSConnector(source="jasmin", max_attempts=2, retry_delay_seconds=2)
    target = connector.discover(scene_date=_CAMS_JASMIN_DATE)[0]
    base = JASMIN_BASE_URL.rstrip("/")
    if not connector._date_available(base, target.date_label):
        raise RuntimeError(f"CAMS JASMIN mirror no longer publishes pinned date {target.date_label} at {base}")
    print(f"CAMS JASMIN: {target.date_label} asset family published at {base}")

    if not os.environ.get("ADS_TOKEN"):
        print("CAMS ADS smoke SKIPPED: ADS_TOKEN not set")
        return
    # BYO ADS token: one day of the three EAC4 variables (small global netCDF).
    with tempfile.TemporaryDirectory() as cache_dir:
        ads = CAMSConnector(cache_dir=cache_dir, source="ads", max_attempts=2)
        result = ads.fetch(ads.discover(scene_date=_CAMS_JASMIN_DATE)[0])
        if result.resolved_source != "ads" or result.stratum != "eac4-reanalysis":
            raise RuntimeError(
                f"CAMS ADS resolved {result.resolved_source!r}/{result.stratum!r}, expected ads/eac4-reanalysis"
            )
        if ads.parse(result).empty or not all(path.stat().st_size > 0 for path in result.files):
            raise RuntimeError("CAMS ADS fetch produced no non-empty assets")
        print(f"CAMS ADS: {[path.name for path in result.files]} <- {result.source_url}")


def smoke_emit_earthaccess() -> None:
    # CMR discovery is public; the protected NetCDF download needs Earthdata
    # Login and is multi-GB, so the smoke is discovery + metadata canonical
    # only, bounded to Cuprite, NV over a closed window.
    from spectraccess.connectors.emit_earthaccess import EMITEarthaccessConnector, target_to_canonical

    targets = EMITEarthaccessConnector().discover(
        product="EMITL2ARFL",
        bbox=(-117.35, 37.40, -117.10, 37.65),
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 12, 31, tzinfo=timezone.utc),
        limit=3,
    )
    if not targets:
        raise RuntimeError("EMIT CMR discovery returned no EMITL2ARFL granules over Cuprite in 2024")
    target = targets[0]
    canonical = target_to_canonical(target)
    if canonical.attrs.get("spectraccess_schema_version") is None:
        raise RuntimeError("EMIT canonical frame is not schema-stamped")
    if not (canonical["unc_status"] == "unknown").all():
        raise RuntimeError("EMIT metadata uncertainty was not labelled unknown")
    print(f"EMIT discovery: {len(targets)} granule(s), newest {target.product_id}, canonical={canonical.shape}")


def smoke_landsat_eodag() -> None:
    from spectraccess.connectors.landsat_eodag.connector import PASSWORD_ENV, USERNAME_ENV

    # USGS M2M needs credentials even for search (EODAG prunes the provider
    # without them), so an unconfigured repo must SKIP, not fail.
    if not os.environ.get(USERNAME_ENV) or not os.environ.get(PASSWORD_ENV):
        print(f"Landsat smoke SKIPPED: {USERNAME_ENV}/{PASSWORD_ENV} not set")
        return
    from spectraccess.connectors.landsat_eodag import LandsatEodagConnector, target_to_canonical

    targets = LandsatEodagConnector().discover(
        bbox=(4.80, 52.30, 4.95, 52.40),
        start=datetime(2024, 5, 1, tzinfo=timezone.utc),
        end=datetime(2024, 5, 31, tzinfo=timezone.utc),
        limit=5,
    )
    if not targets:
        raise RuntimeError("Landsat discovery returned no L1TP targets for the Amsterdam bbox in May 2024")
    canonical = target_to_canonical(targets[0])
    if canonical.attrs.get("spectraccess_schema_version") is None:
        raise RuntimeError("Landsat canonical frame is not schema-stamped")
    if canonical.loc[0, "unc_status"] != "unknown":
        raise RuntimeError("Landsat cloud-cover uncertainty was not labelled unknown")
    print(f"Landsat discovery: {len(targets)} target(s), first {targets[0].title}")


def main() -> int:
    connector = sys.argv[1] if len(sys.argv) > 1 else ""
    if connector == "gsics":
        smoke_gsics()
    elif connector == "modis_viirs_cal":
        smoke_viirs()
    elif connector == "radcalnet":
        smoke_radcalnet()
    elif connector == "sentinel2_cdse":
        smoke_sentinel2_cdse()
    elif connector == "aeronet":
        smoke_aeronet()
    elif connector == "cams":
        smoke_cams()
    elif connector == "emit_earthaccess":
        smoke_emit_earthaccess()
    elif connector == "landsat_eodag":
        smoke_landsat_eodag()
    else:
        raise SystemExit(f"unknown connector {connector!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
