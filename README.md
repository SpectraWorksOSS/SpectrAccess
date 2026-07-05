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
| RadCalNet | Stubbed | Authenticated, manually approved account |

NOAA/NESDIS GSICS products are also mirrored on the EUMETSAT collaboration server's master THREDDS catalog (`nesdisProducts.xml`), so some NESDIS product families may already be reachable via the EUMETSAT connector default even while the canonical NOAA STAR host is down.

Maintainer: SpectraWorks B.V. Built by SpectraWorks, makers of RefCal.

spectrAccess code is licensed under Apache-2.0. Source data remains governed by each external portal's own data terms; see each connector's `DATA_TERMS.md`.

