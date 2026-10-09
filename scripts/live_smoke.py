from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from spectraccess.core.credentials import Credential

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

    connector = RadCalNetConnector(credentials=lambda: Credential("password", os.environ["RADCALNET_PASSWORD"], os.environ["RADCALNET_USERNAME"]))
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
    from spectraccess.connectors.sentinel2_cdse import Sentinel2CDSEConnector, target_to_canonical

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
        ads = CAMSConnector(cache_dir=cache_dir, source="ads", max_attempts=2, credentials=lambda: Credential("token", os.environ["ADS_TOKEN"]))
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
    USERNAME_ENV = "EODAG__USGS__API__CREDENTIALS__USERNAME"
    PASSWORD_ENV = "EODAG__USGS__API__CREDENTIALS__PASSWORD"

    # USGS M2M needs credentials even for search (EODAG prunes the provider
    # without them), so an unconfigured repo must SKIP, not fail.
    if not os.environ.get(USERNAME_ENV) or not os.environ.get(PASSWORD_ENV):
        print(f"Landsat smoke SKIPPED: {USERNAME_ENV}/{PASSWORD_ENV} not set")
        return
    from spectraccess.connectors.landsat_eodag import LandsatEodagConnector, target_to_canonical

    targets = LandsatEodagConnector(credentials=lambda: Credential("password", os.environ[PASSWORD_ENV], os.environ[USERNAME_ENV])).discover(
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


def smoke_olci_cdse() -> None:
    if not os.environ.get("CDSE_USERNAME") or not os.environ.get("CDSE_PASSWORD"):
        print("OLCI CDSE smoke SKIP: CDSE_USERNAME/CDSE_PASSWORD not set")
        return
    from spectraccess.connectors.olci_cdse import OLCICDSEConnector
    from spectraccess.connectors.olci_cdse.connector import OLCITarget, PRODUCT_URL, COLLECTION

    # Public CDSE catalogue metadata: a 129 MB LFR product, 23% land and
    # 15% cloud cover. Only IWV, three annotations and the manifest are fetched.
    product_id = "35dc0868-b343-426d-bd25-5ddc1a0bbd59"
    title = "S3B_OL_2_LFR____20240501T042137_20240501T042437_20250705T000836_0179_092_261_2700_ESA_R_NT_003.SEN3"
    target = OLCITarget(product_id, title, PRODUCT_URL.format(product_id=product_id),
                        datetime.now(timezone.utc), {"Id": product_id, "Name": title, "Collection": COLLECTION})
    connector = OLCICDSEConnector(credentials=lambda: Credential(
        "password", os.environ["CDSE_PASSWORD"], os.environ["CDSE_USERNAME"]))
    with tempfile.TemporaryDirectory() as tmp:
        result = connector.fetch(target, dest=tmp)
        # Bound the canonical sample to a small land area within the granule.
        # Flagged finite values retain their provider QA for this transport proof.
        frame = connector.parse_canonical(result, bbox=(80.3, 16.3, 80.6, 16.6), include_flagged=True)
        if frame.empty or not frame.quantity.eq("atmosphere_mass_content_of_water_vapor").all():
            raise RuntimeError("OLCI smoke returned no canonical IWV rows")
        provided = frame.loc[frame.unc_status == "provided"]
        if provided.empty or not provided.unc_provider.eq("OLCI IWV_unc").all() or frame.unc_k.notna().any():
            raise RuntimeError("OLCI smoke did not preserve the published uncertainty contract")
        if provided.unc_definition.isna().any():
            raise RuntimeError("OLCI smoke returned no provider uncertainty definition")
        print(f"OLCI CDSE: filtered fetch and parse passed for {target.title}: "
              f"{len(frame)} IWV pixels, {len(provided)} with published uncertainty")


def smoke_ngl_gnss() -> None:
    from spectraccess.connectors.ngl_gnss import NGLGNSSConnector

    connector = NGLGNSSConnector(max_bytes=2_000_000)
    target = connector.discover(station="ABMF",day=date(2008,9,2))[0]
    frame = connector.parse_canonical(connector.fetch(target))
    delays = frame.loc[frame.quantity == "zenith_total_delay"]
    if len(delays) != 288 or delays.elevation_m.isna().any():
        raise RuntimeError("NGL smoke did not return a full pinned day and station height")
    if not delays.units.eq("m").all() or not delays.unc_status.eq("provided").all():
        raise RuntimeError("NGL ZTD units/formal errors did not match the contract")
    print(f"NGL GNSS: {len(delays)} ZTD epochs with station metadata from {target.source_url}")


def smoke_cams_pressure() -> None:
    if not os.environ.get("ADS_TOKEN"):
        print("CAMS surface pressure smoke SKIP: ADS_TOKEN not set")
        return
    from spectraccess.connectors.cams import CAMSConnector

    connector = CAMSConnector(source="ads", credentials=lambda: Credential("token", os.environ["ADS_TOKEN"]))
    with tempfile.TemporaryDirectory() as tmp:
        result = connector.fetch_surface_pressure(
            valid_time=datetime(2024,5,1,9,tzinfo=timezone.utc),
            area=(52.5,4.0,51.5,5.0),dest=Path(tmp)/"pressure.nc",
        )
        frame = connector.parse_surface_pressure(result)
        if frame.empty or not frame.quantity.eq("surface_air_pressure").all():
            raise RuntimeError("CAMS pressure smoke returned no canonical pressure rows")
        print(f"CAMS EAC4 surface pressure: {len(frame)} regional cells from {result.source_url}")


def smoke_slstr_cdse() -> None:
    if not os.environ.get("CDSE_USERNAME") or not os.environ.get("CDSE_PASSWORD"):
        print("SLSTR CDSE smoke SKIP: CDSE_USERNAME/CDSE_PASSWORD not set")
        return
    from spectraccess.connectors.slstr_cdse import SLSTRConnector
    # Pinned public catalogue product, 436 MB complete; fetch selects only
    # four bands and annotations. Small Australian bbox within its swath.
    product_id = "a18067ba-e29f-43ff-b16d-f09b81df9e98"
    bbox = (138.0, -39.0, 138.2, -38.8)
    connector = SLSTRConnector(credentials=lambda: Credential(
        "password", os.environ["CDSE_PASSWORD"], os.environ["CDSE_USERNAME"]))
    print("SLSTR CDSE stage discover", flush=True)
    targets = connector.discover(bbox=bbox, start=datetime(2024, 5, 1, 0, 12, tzinfo=timezone.utc),
                                 end=datetime(2024, 5, 1, 0, 16, tzinfo=timezone.utc), limit=100)
    target = next((t for t in targets if t.product_id == product_id), None)
    if target is None:
        raise RuntimeError("pinned SLSTR product absent from catalogue discovery")
    with tempfile.TemporaryDirectory() as tmp:
        print("SLSTR CDSE stage fetch", flush=True)
        result = connector.fetch(target, dest=tmp, channels=("S4", "S7", "S8", "S9"), views=("nadir",))
        print("SLSTR CDSE stage parse", flush=True)
        frame = connector.parse_canonical(result, bbox=bbox)
        print("SLSTR CDSE stage read", flush=True)
        ds = connector.read(result, bbox=bbox, channels=("S4", "S7", "S8", "S9"), views=("nadir",))
        expected = ("S4_radiance_an", "S7_BT_in", "S8_BT_in", "S9_BT_in", "cloud_in", "confidence_in", "time_stamp_in", "solar_zenith_tn", "latitude_tx", "longitude_tx")
        if frame.empty or any(name not in ds or ds[name].size == 0 for name in expected):
            raise RuntimeError("SLSTR real fetch/read missing measurements, flags, time or geometry")
        print(f"SLSTR CDSE: discover, filtered fetch, canonical parse and native read passed: {target.title}")


def smoke_viirs_earthaccess() -> None:
    if not os.environ.get('EARTHDATA_USERNAME') or not os.environ.get('EARTHDATA_PASSWORD'):
        print('VIIRS Earthdata smoke SKIP: EARTHDATA_USERNAME/EARTHDATA_PASSWORD not set')
        return
    from spectraccess.connectors.viirs_earthaccess import VIIRSConnector

    # Public CMR daytime partial swath: 18,557,651 + 3,708,627 bytes.
    pin = 'G2120969416-LAADS'
    bbox = (-99.1, -8.4, -98.9, -8.2)
    connector = VIIRSConnector(credentials=lambda: Credential(
        'password', os.environ['EARTHDATA_PASSWORD'], os.environ['EARTHDATA_USERNAME']))
    print('VIIRS Earthdata: discover', flush=True)
    targets = connector.discover(bbox=bbox, start=datetime(2012, 2, 2, 20, 6, 1, tzinfo=timezone.utc),
        end=datetime(2012, 2, 2, 20, 11, 59, tzinfo=timezone.utc), products=('MOD',), platforms=('SNPP',))
    target = next((t for t in targets if t.product_id == pin), None)
    if target is None:
        raise RuntimeError('pinned VIIRS product absent from CMR discovery')
    if target.day_night_flag != 'Day':
        raise RuntimeError(f'VIIRS M09 smoke requires CMR DayNightFlag=Day; found {target.day_night_flag}')
    with tempfile.TemporaryDirectory() as tmp:
        print('VIIRS Earthdata: fetch matching 02/03 pair', flush=True)
        result = connector.fetch(target, dest=tmp, max_bytes=150_000_000)
        if result.targets[1].product_id != 'G2120964460-LAADS':
            raise RuntimeError('VIIRS geolocation pin changed')
        print('VIIRS Earthdata: read native window', flush=True)
        ds = connector.read(result, bbox=bbox, bands=('M09', 'M15', 'M16'))
        frame = connector.parse_canonical(result)
        expected = ('M09', 'M15', 'M16', 'M09_quality_flags', 'M15_quality_flags', 'M16_quality_flags', 'scan_start_time',
                    'scan_index', 'latitude', 'longitude', 'solar_zenith', 'sensor_zenith')
        if frame.empty or any(name not in ds or ds[name].size == 0 for name in expected):
            raise RuntimeError('VIIRS real fetch/read missing bands, flags, scan time or geometry')
        if not ds.attrs['native_geometry'] or ds.attrs['resampled']:
            raise RuntimeError('VIIRS native geometry contract failed')
        print(f'VIIRS Earthdata: discover/fetch/read passed: {target.title}')


def smoke_fci_eumetsat() -> None:
    if not os.environ.get("EUMETSAT_KEY") or not os.environ.get("EUMETSAT_SECRET"):
        print("FCI EUMETSAT smoke SKIP: EUMETSAT_KEY/EUMETSAT_SECRET not set")
        return
    from spectraccess.connectors.fci_eumetsat import FCIConnector

    product_id = ("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+FCI-1C-RRAD-FDHSI-FD--x-x---x_C_EUMT_"
                  "20241001100223_IDPFI_OPE_20241001100007_20241001100928_N__C_0061_0000")
    bbox = (4.8, 52.3, 4.95, 52.4)
    channels = ("ir_105", "nir_13")
    connector = FCIConnector(credentials=lambda: Credential(
        "password", os.environ["EUMETSAT_SECRET"], os.environ["EUMETSAT_KEY"]))

    def checked(stage, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            http = f"HTTP {status} ({status // 100}xx)" if isinstance(status, int) else "HTTP status unavailable"
            print(f"FCI EUMETSAT smoke FAIL: {getattr(exc, 'stage', stage)}; {http}; {exc}", flush=True)
            raise

    print("FCI EUMETSAT discover: pinned 2024-10-01 10:00 repeat cycle", flush=True)
    targets = checked("discover", connector.discover, bbox=bbox, start=datetime(2024, 10, 1, 10, tzinfo=timezone.utc),
                                 end=datetime(2024, 10, 1, 10, 10, tzinfo=timezone.utc),
                                 products=("l1c",), limit=10)
    target = next((target for target in targets if target.product_id == product_id), None)
    if target is None:
        raise RuntimeError("pinned FCI repeat cycle absent from catalogue discovery")
    with tempfile.TemporaryDirectory() as tmp:
        print("FCI EUMETSAT fetch: intersecting body entries and trailer", flush=True)
        result = checked("fetch", connector.fetch, target, dest=tmp, bbox=bbox, channels=channels)
        print("FCI EUMETSAT read: native radiance, acquisition time and quality", flush=True)
        ds = checked("read", connector.read, result, channels=channels)
        expected = [name for channel in channels for name in (channel, channel + "_time", channel + "_pixel_quality")]
        if any(name not in ds or ds[name].size == 0 for name in expected):
            raise RuntimeError("FCI real fetch/read missing radiance, pixel time or quality")
        if any(not bool(ds[channel].notnull().any()) for channel in channels):
            raise RuntimeError("FCI real fetch/read returned no valid radiances")
        if any(not bool(ds[channel + "_time"].notnull().any()) for channel in channels):
            raise RuntimeError("FCI real fetch/read returned no valid acquisition times")
        frame = checked("canonical parse", connector.parse_canonical, result, channels=channels)
        if frame.empty or frame.attrs.get("spectraccess_schema_version") != "1.0":
            raise RuntimeError("FCI smoke returned no validated canonical repeat cycle")
        print(f"FCI EUMETSAT: discover, filtered fetch, canonical parse and native read passed: {target.title}")


def smoke_seviri_eumetsat() -> None:
    if not os.environ.get("EUMETSAT_KEY") or not os.environ.get("EUMETSAT_SECRET"):
        print("SEVIRI EUMETSAT smoke SKIP: EUMETSAT_KEY/EUMETSAT_SECRET not set")
        return
    import numpy as np
    from spectraccess.connectors.seviri_eumetsat import SEVIRIConnector

    product_id = "MSG2-SEVI-MSG15-0100-NA-20160615100416.823000000Z-NA"
    connector = SEVIRIConnector(credentials=lambda: Credential(
        "password", os.environ["EUMETSAT_SECRET"], os.environ["EUMETSAT_KEY"]))

    def checked(stage, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Exception as exc:
            print(f"SEVIRI EUMETSAT smoke FAIL: {getattr(exc, 'stage', stage)}; "
                  f"HTTP {getattr(exc, 'status_code', 'unavailable')}; {exc}", flush=True)
            raise

    print("SEVIRI EUMETSAT discover: both services, 2016-06-15 10:00-10:20 UTC", flush=True)
    targets = checked("discover", connector.discover, bbox=(-10, 35, 30, 60),
                      start=datetime(2016, 6, 15, 10, tzinfo=timezone.utc),
                      end=datetime(2016, 6, 15, 10, 20, tzinfo=timezone.utc))
    if {t.service for t in targets} != {"full_disc", "rapid_scan"}:
        raise RuntimeError("both SEVIRI services must be present in catalogue discovery")
    target = next((t for t in targets if t.product_id == product_id), None)
    if target is None:
        raise RuntimeError("pinned RSS product absent from catalogue discovery")
    with tempfile.TemporaryDirectory() as tmp:
        print(f"SEVIRI EUMETSAT fetch: whole RSS native product {product_id}", flush=True)
        result = checked("fetch", connector.fetch, target, dest=tmp)
        lines = checked("line_times", connector.line_times, result, channel="IR_108")
        measured = lines.time.values.astype("datetime64[ns]")
        valid = ~np.isnat(measured)
        row = lines.row.values.astype(np.int64)
        steps = np.diff(row)
        print(f"SEVIRI RSS line side info: rows={len(row)}; timed={int(valid.sum())}; "
              f"first rows={row[:3].tolist()}; last rows={row[-3:].tolist()}; "
              f"non-increasing steps={int(np.sum(steps <= 0))}; "
              f"line_validity values={np.unique(lines.line_validity.values).tolist()}", flush=True)
        if not valid.any():
            raise RuntimeError("no provider line acquisition time in the native product")
        if not np.all(steps > 0):
            bad = np.flatnonzero(steps <= 0)[:5]
            raise RuntimeError(f"ICD grid rows not strictly increasing at array rows {bad.tolist()}: "
                               f"{[(int(row[i]), int(row[i + 1])) for i in bad]}")
        times = measured[valid]
        begin = np.datetime64(target.sensing_start.replace(tzinfo=None), "ns")
        end = np.datetime64(target.sensing_end.replace(tzinfo=None), "ns")
        timed_rows = row[valid]
        step_seconds = np.diff(times) / np.timedelta64(1, "s")
        backwards = np.flatnonzero(step_seconds < 0)
        print(f"SEVIRI RSS line times: first={times[0]}; last={times[-1]}; catalogue interval={begin} to {end}; "
              f"before start={int(np.sum(times < begin))}; after end={int(np.sum(times > end))}; "
              f"median step seconds={float(np.median(step_seconds)):.6f}; backwards steps={len(backwards)}", flush=True)
        for i in backwards:
            print(f"SEVIRI RSS backwards line time: rows {int(timed_rows[i])}->{int(timed_rows[i + 1])}; "
                  f"{times[i]} -> {times[i + 1]}; step seconds={float(step_seconds[i]):.6f}", flush=True)
        if np.any(times < begin) or np.any(times > end):
            raise RuntimeError("provider acquisition times must lie inside the catalogue sensing interval")
        # Provider mean line times are reported unmodified. A backwards step larger than
        # about two rapid-scan swaths would mean misordered lines, not timing jitter.
        if backwards.size and float(-step_seconds[backwards].min()) > 1.0:
            raise RuntimeError("provider acquisition times run backwards by more than 1 s between grid rows")
        coverage = lines.attrs["actual_coverage"]["VIS_IR"]
        declared = target.service_coverage
        actual_first, actual_last = int(coverage["SouthernLineActual"]), int(coverage["NorthernLineActual"])
        print(f"SEVIRI RSS actual L1.5 coverage: {actual_first}-{actual_last}; "
              f"documented nominal: {declared['south_line']}-{declared['north_line']}; "
              f"bound differences: {actual_first - declared['south_line']}, {actual_last - declared['north_line']}; "
              f"timed rows: {int(valid.sum())}/{len(row)}", flush=True)
        rows_in_service = declared["north_line"] - declared["south_line"] + 1
        duration = (end - begin) / np.timedelta64(1, "s")
        nominal_seconds = (row[valid] - declared["south_line"]) / rows_in_service * duration
        measured_seconds = (times - begin) / np.timedelta64(1, "s")
        residual = measured_seconds - nominal_seconds
        print(f"SEVIRI RSS scan-law residual seconds: max={float(np.max(np.abs(residual))):.6f}; "
              f"rms={float(np.sqrt(np.mean(residual ** 2))):.6f}; "
              "nominal = sensing_start + (ICD row - documented first row) / documented service rows * sensing duration", flush=True)
        if not lines.attrs["satellite_actual_position_available"]:
            raise RuntimeError("native header has no usable actual orbital position")
        print(f"SEVIRI native orbital parameters: {lines.attrs['orbital_parameters']}", flush=True)
        bbox = (4.8, 52.3, 4.95, 52.4)
        for channel, calibration in (("IR_108", "radiance"), ("VIS006", "counts")):
            ds = checked("read", connector.read, result, bbox=bbox, channels=(channel,), calibration=calibration)
            if not ds[channel].size or not bool(ds[channel].notnull().any()):
                raise RuntimeError(f"SEVIRI {channel} {calibration} Netherlands window has no valid values")
            if not ds.attrs["native_geometry"] or ds.attrs["resampled"]:
                raise RuntimeError("SEVIRI native geometry contract failed")
        print(f"SEVIRI EUMETSAT PASS: discovery, fetch, measured line time, coverage, native radiance/counts: {product_id}")


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
    elif connector == "olci_cdse":
        smoke_olci_cdse()
    elif connector == "slstr_cdse":
        smoke_slstr_cdse()
    elif connector == "viirs_earthaccess":
        smoke_viirs_earthaccess()
    elif connector == "seviri_eumetsat":
        smoke_seviri_eumetsat()
    elif connector == "fci_eumetsat":
        smoke_fci_eumetsat()
    elif connector == "ngl_gnss":
        smoke_ngl_gnss()
    elif connector == "cams_pressure":
        smoke_cams_pressure()
    else:
        raise SystemExit(f"unknown connector {connector!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
