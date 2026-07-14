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
