# spectrAccess

spectrAccess is a Python client that finds and downloads spectral and atmospheric reference data from RadCalNet, GSICS, AERONET, CAMS, Landsat, Sentinel-2 and NASA EMIT, and parses metadata into pandas DataFrames. GSICS, RadCalNet and AERONET measurements can also be returned in one shared schema where every value carries an uncertainty record and its source, URL and retrieval time (see Canonical schema below). Sentinel-2 and Landsat return scene metadata. EMIT also reads local reflectance cubes into xarray Datasets. CAMS returns a table of the downloaded files.

It is written for remote sensing and Earth observation scientists, calibration engineers and satellite data providers who spend too much time on portal logins, file formats and unit conventions instead of the comparison itself.

spectrAccess fetches data on your behalf, using your own free account where a source needs one. It never re-serves or redistributes source data; each connector's `DATA_TERMS.md` lists that provider's terms and citation requirements.

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

For EMIT L1B/L2A discovery and download through NASA earthaccess:

```bash
pip install "spectraccess[emit]"
```

The EMIT extra requires Python 3.12 or newer (the requirement of the reviewed
`earthaccess==0.18.0` client). The base package and other extras continue to
support the Python versions declared in the package metadata.

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

## Credentials

Run `spectraccess login <provider>` once for personal access, or hand over a
credential source in code. See [credentials and migration to 0.2](docs/credentials.md).
Credential environment variables and provider-client login files are no longer read.

## Connectors

| Connector | Status | Auth requirement |
| --- | --- | --- |
| GSICS GPPA | Available (EUMETSAT live, verified end-to-end; CMA catalog live but content-empty as of 2026-07-05; NOAA STAR pending, host unreachable 2026-07-05) | None for public THREDDS catalogs |
| MODIS/VIIRS calibration LUT ([VIIRS](https://eo-atlas.org/products/sensor/viirs), [MODIS](https://eo-atlas.org/products/sensor/modis)) | VIIRS connector shape available; NOAA STAR F-factor THREDDS URL pending verification; MODIS planned | None for public VIIRS THREDDS; MODIS source design pending |
| RadCalNet | Available (official JSON API; checked by hand against the live portal, 2026-07) | Free portal account; see [credentials](docs/credentials.md) |
| Sentinel-2 CDSE ([MSI](https://eo-atlas.org/products/sensor/msi)) | Available (thin adapter over maintained `cdsetool`; public discovery, BYO-credential download) | None for catalogue discovery; free CDSE account for product download; see [credentials](docs/credentials.md) |
| NASA AERONET v3 | Available (native client for the public v3 web service; per-band AOD, Angstrom exponent, precipitable water; 550 nm AOD interpolation helper) | None; cite AERONET and the site PI (see `DATA_TERMS.md`) |
| CAMS EAC4 / JASMIN ([CAMS forecast](https://eo-atlas.org/data_products/cams-global-forecast)) | Available (JASMIN cache access plus thin ADS adapter over maintained `cdsapi`; explicit CAMS forecast product for dates EAC4 does not yet cover) | None for JASMIN, whose public mirror currently holds files only up to 2025-10-03; free ADS account/token for later dates and automatic fallback; see [credentials](docs/credentials.md) |
| NASA EMIT L1B/L2A ([EMIT](https://eo-atlas.org/products/sensor/emit)) | Available (thin adapter over maintained `earthaccess`) | None for CMR discovery; free Earthdata Login for protected NetCDF download; see [credentials](docs/credentials.md) |
| Landsat 8/9 Collection 2 L1TP ([OLI](https://eo-atlas.org/products/sensor/oli), [OLI-2](https://eo-atlas.org/products/sensor/oli-2)) | Available (thin adapter over maintained EODAG USGS plugin; preserves tier and WRS-2 identity) | Free USGS EarthExplorer account and M2M application token; see [credentials](docs/credentials.md) |

Sensor links in the table go to [EO-Atlas](https://eo-atlas.org/), SpectraWorks' open catalogue of Earth observation satellites, sensors and data products, for background on each instrument.

NOAA/NESDIS GSICS products are also mirrored on the EUMETSAT collaboration server's master THREDDS catalog (`nesdisProducts.xml`), so some NESDIS product families may already be reachable via the EUMETSAT connector default even while the canonical NOAA STAR host is down.

Landsat (USGS), RadCalNet, and CAMS via ADS have fixture tests, but they have
not yet been exercised against the live services in CI: they need account
credentials, and the weekly live checks skip them until those are configured.
The free JASMIN CAMS mirror has no files after 2025-10-03, so recent CAMS dates
need an ADS account and token.

## Canonical schema

Alongside each connector's native `parse()` output, connectors can additionally emit
a shared, versioned, long/tidy canonical schema (`spectraccess.core.schema`, currently
`SCHEMA_VERSION = "1.0"`) so downstream tools can consume any source through one stable
contract: one row per quantity value plus its uncertainty record. GSICS, RadCalNet and
AERONET expose this via `to_canonical(native_frame, ...)` and the connector convenience
method `parse_canonical(raw, ...)`; the Sentinel-2 CDSE, EMIT and Landsat connectors emit
canonical scene metadata via `target_to_canonical(target)`. CAMS returns its native
frame only.

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

- `provided`: the source itself supplied the uncertainty.
- `derived`: computed from other quantities by a downstream tool (no connector emits this yet).
- `prior`: an assumed/prior uncertainty, not measured for this row.
- `unknown`: no uncertainty value is available (`unc_value` is null).

RadCalNet `.output` files carry an absolute, dimensionless uncertainty value
for each wavelength and observation. The native frame preserves it as
`toa_reflectance_unc` together with `toa_reflectance_unc_status`,
`toa_reflectance_unc_provider`, and `toa_reflectance_unc_k`. Positive source
values are `provided`; negative values are climatological magnitudes and are
therefore `prior`; fill or absent values are `unknown`. RadCalNet R2 does not
state a coverage factor, so `toa_reflectance_unc_k` and canonical `unc_k` stay
null. spectrAccess never substitutes a fixed percentage or assumes `k=1`.
`.input` files hold the surface reflectance the TOA values were propagated
from, so they parse to `surface_reflectance` and its matching `_unc` columns
(canonical `quantity="surface_reflectance"`).

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
Constructor arguments override environment variables: `CAMS_SOURCE`,
`CAMS_FORECAST_CYCLE`, `CAMS_FORECAST_LEAD_HOURS`,
`SPECTRACCESS_CAMS_CACHE_DIR` (default `~/.cache/spectraccess/cams`), and
`SPECTRACCESS_CAMS_FALLBACK_URL` (a second mirror with the JASMIN layout).

The EMIT connector's canonical output covers source-provided scene metadata (cloud cover and
solar angles), each labelled `unc_status="unknown"` because CMR does not publish
an uncertainty for those metadata values. Exact collection/native IDs, footprint,
orbit/scene, asset URLs, byte sizes, and SHA-512 checksums remain in the target
provenance. Scientific use remains a downstream decision.

For local EMIT L2A files, `read_cube(reflectance, *, uncertainty=None, mask=None,
observation=None, target=None)` returns a lazy `xarray.Dataset`. It is also
available as `EMITEarthaccessConnector.read_cube`. Install `spectraccess[emit]`
for the h5netcdf backend. Close the dataset after use, or use a context manager:

```python
from spectraccess.connectors.emit_earthaccess import read_cube

# Paths to files already downloaded from NASA LP DAAC.
with read_cube("RFL.nc", uncertainty="RFL_UNCERT.nc",
               mask="MASK.nc", observation="OBS.nc") as cube:
    spectrum = cube.reflectance.isel(downtrack=0, crosstrack=0).values
    print(cube.wavelengths.values, spectrum)
```

`reflectance` and `reflectance_uncertainty` share raw `(downtrack, crosstrack,
bands)` indices. Reflectance and its uncertainty use the provider's units
(dimensionless reflectance); `wavelengths` and `fwhm` carry the granule's units
(nm). `good_wavelengths` is a flag array. Its zero channels retain the published
`-0.01` placeholder. Only fill values become NaN, with `-9999` as the
reflectance and uncertainty fill. Bands are neither removed nor resampled.

The `location` group's raw `lat` and `lon` are coordinates, with provider units
of degrees. GLT arrays retain their published integer values, dimensions and
fill (`-9999` in V001, `0` in V002). No index base is inferred and no
orthorectification is performed. Global file metadata, including
`product_version`, binds the band parameters to this granule.

`mask` has `(downtrack, crosstrack, mask_bands)` dimensions. For V001,
provider labels `AOD550` and `H2O (g cm-2)` select `aerosol_optical_depth` and
`water_vapor` case-insensitively. Only when no labels are published does the
reader use ATBD channels 6 and 7 (zero-based indices 5 and 6). Each alias records
`alias_source` as `provider mask_bands label` or `ATBD channel order`.
Published labels that fail to identify both channels raise `ValueError`.
V002 accepts the separate EMITL2AMASK product and preserves its
published value arrays. Band labels are preserved as `mask_<provider_name>`.
`obs` has `(downtrack, crosstrack, observation_bands)` dimensions, with geometry
band labels in `obs_<provider_name>` (including solar/view zenith and azimuth).
Units and labels come from the files; no angles are derived.

The uncertainty attributes quote NASA's L2A ATBD: "Reflectance uncertainty
(one standard deviation)" and "predicted uncertainty in the reflectance
measurement for each channel, in units of standard deviations (presuming a
Gaussian distribution)." No further uncertainty interpretation is added.
The latter quote is in `provider_definition`; existing provider descriptions
and long names are preserved.
Omitted optional files produce no corresponding variables; an explicitly
supplied missing file raises an error. Companion versions and raw dimensions
must match. Provider scene identifiers are checked when present.

`parse` and `parse_canonical` still return metadata DataFrames. When given an
existing local file, they share `read_product_metadata` with the cube reader
and check its version against the CMR target. Passing `target` to `read_cube`
performs the same check. Discovery and local reading support V001 and V002.

EMIT discovery uses NASA CMR collection short names. Companion files are
selected from a discovered target with `fetch(target, dest=..., asset=...)`:

| Collection | Versions | Assets and fetch selectors |
| --- | --- | --- |
| EMITL2ARFL | 001, 002 | RFL (`primary`), RFLUNCERT (`uncertainty`); MASK (`mask`) in 001 only |
| EMITL1BRAD | 001, 002 | RAD (`primary`), OBS (`observation`) |
| EMITL2AMASK | 002 | MASK (`primary` or `mask`) |

`discover(product="EMITL2ARFL", version="002", ...)` selects V002 explicitly.
Omitting `version` retains V001 for RFL and RAD and selects V002 for the standalone
mask collection. RFLUNCERT and OBS are companion assets in both versions,
rather than separate `EMITL2ARFLUNCERT` or `EMITL1BOBS` collections. For V002,
discover the mask separately as `product="EMITL2AMASK", version="002"`.
Fetch continues to download one selected file and verify its provider checksum.
See the [NASA CMR sources and filename prefixes](src/spectraccess/connectors/emit_earthaccess/DATA_TERMS.md#collection-and-asset-layout)
for the published collection layout.

The Landsat connector applies the same boundary to Collection-2 L1TP products:
EODAG owns USGS search, authentication, retries, and download transport;
spectrAccess preserves the provider product ID, display ID, collection number,
tier (`T1`, `T2`, or `RT`), WRS-2 path/row, footprint, provider metadata, and a
stable cache identifier. Its canonical row describes only provider scene cloud
cover, with unknown uncertainty; archive pixels remain a downstream concern.

Call `spectraccess.core.schema.validate(df)` to check a frame against the schema; it raises
`SchemaError` naming every violation found. Extra, connector-specific columns are always
allowed and pass through validation untouched.

Maintainer: SpectraWorks B.V. Built by SpectraWorks, makers of [RefCal](https://spectraworks.nl/refcal), the cross-sensor calibration layer. Also from SpectraWorks: [EO-Atlas](https://eo-atlas.org/), a catalogue of Earth observation satellites, sensors and data products.

spectrAccess code is licensed under Apache-2.0. Source data remains governed by each external portal's own data terms; see each connector's `DATA_TERMS.md`.

