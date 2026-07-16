# Changelog

All notable changes to spectrAccess are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-07-17

Initial public release.

### Added

- Core `Connector` interface (`discover()`, `fetch()`, `parse()`) for
  source-specific spectral reference data adapters.
- GSICS connector for GPPA inter-calibration products (EUMETSAT, CMA
  catalogs; NOAA STAR optional override).
- RadCalNet connector with BYO-credentials portal access.
- Sentinel-2 discovery/download connector via CDSETool (`spectraccess[cdse]`).
- CAMS EAC4 connector via ECMWF's CDS/ADS API (`spectraccess[cams]`).
- EMIT L1B/L2A connector via NASA earthaccess (`spectraccess[emit]`,
  requires Python >=3.12).
- Landsat Collection-2 connector via EODAG/USGS (`spectraccess[landsat]`).
- Weekly live-smoke workflow (`.github/workflows/live-smoke.yml`) that opens
  or updates a `connector-broken` issue on failure.
- Fixture-based CI test suite (`.github/workflows/ci.yml`).

[0.1.0]: https://github.com/SpectraWorksOSS/SpectrAccess/releases/tag/v0.1.0
