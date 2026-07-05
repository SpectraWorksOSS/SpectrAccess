# Contributing

Community contributions are welcome, especially new connectors for spectral reference data portals that lack maintained Python access layers.

## Add a Connector

1. Create a package under `src/spectraccess/connectors/<source_name>/`.
2. Implement the `spectraccess.core.connector.Connector` interface:
   `discover()` lists available targets, `fetch()` retrieves raw bytes or files,
   and `parse()` converts raw content into a tidy `pandas.DataFrame` or
   `xarray.Dataset`.
3. Add a `DATA_TERMS.md` file in the connector package describing the source
   portal's data license or terms of use. The Apache-2.0 project license covers
   code only, not source data.
4. Add fixture-based tests under `tests/` using small recorded files checked
   into `tests/fixtures/`. Default pytest runs must not make live network calls.
5. Add or update live-smoke coverage in `.github/workflows/live-smoke.yml` for
   public endpoints that should be monitored weekly.

Do not hardcode credentials or redistribute third-party data through this
project. Connectors should use the BYO-credentials helpers in
`spectraccess.core.session` when authentication is required.

