# Changelog

All notable changes to spectrAccess are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

Initial public release.

### Added

- Core `Connector` interface (`discover()`, `fetch()`, `parse()`, `run()`) for
  source-specific spectral reference data adapters.
- Canonical long/tidy schema v1.0 (`spectraccess.core.schema`): one row per
  quantity value with an uncertainty record whose status (`provided`,
  `prior`, `unknown`) is never null, even when the value is.
- GSICS connector for GPPA inter-calibration products (EUMETSAT and CMA
  catalogs; NOAA STAR as an optional override).
- VIIRS calibration connector shape (NOAA STAR F-factor catalog pending
  verification; MODIS planned).
- RadCalNet connector on the official JSON API with BYO portal credentials,
  including per-wavelength uncertainties mapped to `provided` / `prior`;
  `.output` files parse to `toa_reflectance`, `.input` files to
  `surface_reflectance`.
- NASA AERONET v3 connector for the public web service, with a 550 nm AOD
  interpolation helper.
- Sentinel-2 L1C discovery and download via CDSETool (`spectraccess[cdse]`).
- CAMS EAC4 access via the JASMIN mirror and ECMWF's ADS API, plus explicit
  CAMS forecast products (`spectraccess[cams]`).
- NASA EMIT L1B/L2A discovery and download via earthaccess
  (`spectraccess[emit]`, requires Python 3.12 or newer).
- Landsat 8/9 Collection-2 L1TP discovery and download via EODAG/USGS
  (`spectraccess[landsat]`); area searches skip non-L1TP products the
  provider returns alongside.
- Weekly live-smoke workflow that opens or updates a `connector-broken` issue
  when a portal stops answering as expected.
- Fixture-based CI test suite on Python 3.10 to 3.13.
- `py.typed` marker: the public API ships inline type hints.

[0.1.0]: https://github.com/SpectraWorksOSS/SpectrAccess/releases/tag/v0.1.0
