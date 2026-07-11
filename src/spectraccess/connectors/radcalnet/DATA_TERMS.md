# RadCalNet Data Terms

## Data Policy (v1.1, quoted from the RadCalNet portal, fetched 2026-07-07)

> The RadCalNet data distributed through the RadCalNet portal are freely and
> publically available.

Users of RadCalNet data MUST acknowledge the contribution of RadCalNet, and if
appropriate any specific sites, in any presentation or publication. Specimen
citation given by the portal:

> Calibration data for this "work" came from the CEOS WGCV RadCalNet service
> (https://www.radcalnet.org/) and in particular from site X and Y.

The Data Policy also carries a best-efforts disclaimer: data quality and
interpretation are the responsibility of the individual site owners, and the
data are valid only at nadir and at the stated observation times. Users should
consult with the site owners referenced in the specimen citation regarding
appropriate use of the data.

Also cite the RadCalNet reference publication:

> Bouvet, M., et al. (2019). RadCalNet: A Radiometric Calibration Network for
> Earth Observing Imagers Operating in the Visible to Shortwave Infrared
> Spectral Range. Remote Sensing, 11(20), 2401.
> https://doi.org/10.3390/rs11202401

## Account and credentials

A RadCalNet portal account is free, self-service registration (contact:
admin-radcalnet@magellium.fr for account issues). spectrAccess is bring-your-
own-credentials: it ships no RadCalNet data beyond one trimmed test fixture
(see `tests/fixtures/README.md`) and no credentials of its own. Set
`RADCALNET_USERNAME` / `RADCALNET_PASSWORD` in your own environment; the
connector never logs or echoes these values.

That fixture is a trimmed real RadCalNet file
redistributed under RadCalNet Data Policy v1.1, which permits redistribution
with acknowledgment. We acknowledge RadCalNet and the GONA/GSCN Baotou site
operators. The fixture is DATA and is not covered by the repository's
Apache-2.0 code licence.

## Format and API authority

- File-format authority: the official R2-DataFormatSpecification (V10),
  published on the RadCalNet portal. It documents the fill-value family and
  the negative-value ("average or climatological value") convention that this
  connector's parser implements.
- API reference: the portal's own official `radcalnet_api_client.py` reference
  client (Magellium, 2022) and tech note ACTION-TN-074-MAG document the JSON
  API this connector calls. That reference client's code is "All rights
  reserved" and is not used or copied here -- this connector implements the
  documented API shape independently.

Users must comply with RadCalNet's account, download, citation, and data-use
terms for any files they fetch with their own approved credentials.
