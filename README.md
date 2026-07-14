# spectrAccess

spectrAccess is a general-purpose Python client for hard-to-reach spectral reference data: it gives spectral-data users one coherent package of source-specific connectors for discovering, fetching, and parsing files from portals that do not currently have maintained Python access layers. It uses a BYO-credentials model where needed, so it fetches data on the user's behalf and does not re-serve or redistribute source data.

## Install

```bash
pip install spectraccess
```

For local development:

```bash
pip install -e ".[test]"
```

Large-archive adapters keep their maintained provider clients optional. For
Sentinel-2 discovery and download through CDSETool:

```bash
pip install "spectraccess[cdse]"
```

For CAMS EAC4 access through ECMWF's maintained CDS API client:

```bash
pip install "spectraccess[cams]"
```

For Landsat Collection-2 discovery and download through EODAG/USGS:

```bash
pip install "spectraccess[landsat]"
```

## Quickstart

```python
from spectraccess.connectors.gsics import GSICSConnector

connector = GSICSConnector()
datasets = connector.discover()
target = datasets[0]
raw = connector.fetch(target)
table = connector.parse(raw)
print(table.head())
```

## Connectors

| Connector | Status | Auth requirement |
| --- | --- | --- |
| GSICS GPPA | Available (EUMETSAT live, verified end-to-end; CMA catalog live but content-empty as of 2026-07-05; NOAA STAR pending, host unreachable 2026-07-05) | None for public THREDDS catalogs |
| MODIS/VIIRS calibration LUT | VIIRS connector shape available; NOAA STAR F-factor THREDDS URL pending verification; MODIS planned | None for public VIIRS THREDDS; MODIS source design pending |
| RadCalNet | Available (official JSON API; live-verified) | Free portal account; HTTP Basic auth via BYO credentials |
| Sentinel-2 CDSE | Available (thin adapter over maintained `cdsetool`; public discovery, BYO-credential download) | None for catalogue discovery; free CDSE account for product download |
| CAMS EAC4 / JASMIN | Available (JASMIN cache access plus thin ADS adapter over maintained `cdsapi`) | None for JASMIN; free ADS account/token for ADS and automatic fallback |
| Landsat 8/9 Collection 2 L1TP | Available (thin adapter over maintained EODAG USGS plugin; preserves tier and WRS-2 identity) | Free USGS EarthExplorer account and M2M application token via BYO credentials |

NOAA/NESDIS GSICS products are also mirrored on the EUMETSAT collaboration server's master THREDDS catalog (`nesdisProducts.xml`), so some NESDIS product families may already be reachable via the EUMETSAT connector default even while the canonical NOAA STAR host is down.

## Canonical schema

Alongside each connector's native `parse()` output, connectors can additionally emit
a shared, versioned, long/tidy canonical schema (`spectraccess.core.schema`, currently
`SCHEMA_VERSION = "1.0"`) so downstream tools can consume any source through one stable
contract: one row per quantity value plus its uncertainty record. GSICS and RadCalNet
expose this via `to_canonical(native_frame, ...)` and the connector convenience method
`parse_canonical(raw, ...)`.

| column | meaning |
| --- | --- |
| `time`, `platform`, `instrument`, `band`, `wavelength_nm` | observation identity |
| `site`, `latitude`, `longitude` | ground-site location, when applicable |
| `reference` | reference sensor/standard for differential quantities |
| `quantity` (required, never null) | snake_case quantity name (CF `standard_name` when one exists) |
| `value`, `units` | the measurement |
| `unc_value`, `unc_status`, `unc_k`, `unc_provider` | the uncertainty record (see below) |
| `source` (required, never null), `source_agency`, `source_url`, `retrieved_at` | provenance |

Uncertainty is a record, not a bare number: `unc_value` may be null, but `unc_status` never
is. `unc_status` is one of:

- `provided` -- the source itself supplied the uncertainty.
- `derived` -- computed by spectrAccess or a downstream tool.
- `prior` -- an assumed/prior uncertainty, not measured for this row.
- `unknown` -- no uncertainty value is available (`unc_value` is null).

RadCalNet `.output` files carry an absolute, dimensionless uncertainty value
for each wavelength and observation. The native frame preserves it as
`toa_reflectance_unc` together with `toa_reflectance_unc_status`,
`toa_reflectance_unc_provider`, and `toa_reflectance_unc_k`. Positive source
values are `provided`; negative values are climatological magnitudes and are
therefore `prior`; fill or absent values are `unknown`. RadCalNet R2 does not
state a coverage factor, so `toa_reflectance_unc_k` and canonical `unc_k` stay
null. spectrAccess never substitutes a fixed percentage or assumes `k=1`.

The Sentinel-2 CDSE connector canonicalizes the provider's scene cloud-cover
metadata as `quantity="scene_cloud_cover"`. CDSE does not publish a numerical
uncertainty for that field, so it is honestly labelled `unc_status="unknown"`
with null `unc_value`, `unc_k`, and `unc_provider`. Product identity, footprint,
processing version, catalogue/download URLs, and the exact provider metadata
remain attached as provenance. spectrAccess does not parse SAFE pixels or
reimplement CDSE transport; those stay with CDSETool and downstream consumers.

The CAMS connector returns a `CAMSResult` that names `base_dir` (the directory
containing the date subtree) and `date_dir` (the `YYYY_MM_DD` subtree)
separately. This is an intentional typed contract: consumers such as SIAC that
append the date must use `base_dir`, while format converters can work inside
`date_dir`. `requested_source`, `resolved_source`, dataset URL, retrieval time,
cache status, and exact local assets are retained as native provenance. The
connector retrieves source assets only; atmospheric-correction and
model-specific format conversion remain downstream responsibilities.

The Landsat connector applies the same boundary to Collection-2 L1TP products:
EODAG owns USGS search, authentication, retries, and download transport;
spectrAccess preserves the provider product ID, display ID, collection number,
tier (`T1`, `T2`, or `RT`), WRS-2 path/row, footprint, provider metadata, and a
stable cache identifier. Its canonical row describes only provider scene cloud
cover, with unknown uncertainty; archive pixels remain a downstream concern.

Call `spectraccess.core.schema.validate(df)` to check a frame against the schema; it raises
`SchemaError` naming every violation found. Extra, connector-specific columns are always
allowed and pass through validation untouched.

Maintainer: SpectraWorks B.V. Built by SpectraWorks, makers of RefCal.

spectrAccess code is licensed under Apache-2.0. Source data remains governed by each external portal's own data terms; see each connector's `DATA_TERMS.md`.

