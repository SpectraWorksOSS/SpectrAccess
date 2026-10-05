# CAMS data terms

This connector is Apache-2.0 code. CAMS source data remain governed by the
Copernicus data licence and ECMWF/Atmosphere Data Store terms. Users bring
their own ADS account and personal access token; spectrAccess does not proxy,
re-serve, or bundle CAMS data.

- Copernicus data licence: https://www.copernicus.eu/en/access-data/copernicus-data-and-information-policy
- Atmosphere Data Store terms: https://apps.ecmwf.int/datasets/licences/copernicus/
- ADS API setup: https://ads.atmosphere.copernicus.eu/how-to-api

The JASMIN path accesses STFC/NERC's public NCEO ARD mirror. It is treated as
a best-effort source and its resolved source URL is retained in every result.
The ADS path identifies `cams-global-reanalysis-eac4` and retains the official
dataset/API URL. Publications should acknowledge CAMS and Copernicus in line
with the applicable source terms.

## Surface pressure

`fetch_surface_pressure(valid_time=..., area=..., dest=...)` uses this same
ADS account/token and `cams-global-reanalysis-eac4`, which explicitly lists
`surface_pressure` in its official request constraints:
https://ads.atmosphere.copernicus.eu/api/catalogue/v1/collections/cams-global-reanalysis-eac4/constraints.json
ERA5/CDS is not needed for this connector addition. Accept the EAC4 licence
in your ADS account before requesting data. Product reference:
https://ads.atmosphere.copernicus.eu/datasets/cams-global-reanalysis-eac4

Requests use one exact three-hour analysis epoch and a regional area in
north, west, south, east order. Existing aerosol/water-vapour/ozone retrievals
and their result shape remain unchanged. `CAMSSurfacePressureResult` preserves
the dataset, exact request, URL and retrieval time. There is no automatic
forecast substitution for dates EAC4 does not cover.

`parse_surface_pressure` emits `surface_air_pressure` in Pa with valid time,
grid-cell support and `correlation_groups=['G-MOD']`. Non-finite values are
omitted; non-positive pressure or incompatible units fail explicitly. EAC4
does not publish a per-cell pressure sigma, so uncertainty is unknown with
no k or likelihood assumed. No uncertainty split or assimilated-input list
is guessed. EAC4's model assimilation is described by the dataset, but no
observation prior state/covariance or averaging kernel is published in these
pressure files; those metadata stay absent.

Coordinate bounds and elevation are retained only when the file provides
them. Cell centres alone do not imply published cell boundaries or model
surface elevation. Sampling spacing is not an integration window.
Collection version is EAC4; algorithm version is retained only when supplied
by the file. Publication and usage conditions remain those of CAMS above.

Usage and connector release notes: [atmospheric connectors](../../../../docs/atmospheric-connectors.md).
