# Sentinel-3 OLCI IWV access and data terms

Code is Apache-2.0. Source data remain governed by the Sentinel Data Legal
Notice and Copernicus Data Space Ecosystem service terms. Sentinel access is
free, full and open subject to source credit and fair-use quotas.

- https://dataspace.copernicus.eu/terms-and-conditions
- https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice
- https://documentation.dataspace.copernicus.eu/Quotas.html
- Product definitions: https://sentiwiki.copernicus.eu/web/olci-products
- Product format specification, section 4.2.1.1: https://sentiwiki.copernicus.eu/__attachments/a_6f176f578b4da9de30543ce4640d39836d5df877b47e15dd9dee306902616fd2/S3IPF-PDS-004.2-Sentinel-3-OLCI-Level-2-Land-Product-Data-Format-Specification-2.4.pdf
- Validation: https://doi.org/10.5194/amt-15-5129-2022

Discovery is public. Downloads use your CDSE account through CDSETool:
explicit username/password, a Credentials object, `CDSE_USERNAME` and
`CDSE_PASSWORD`, or CDSETool's `.netrc` support. Credentials are not returned
in metadata. The connector downloads only IWV, geolocation, line times and
land quality flags from `OL_2_LFR___` products, not every scene asset.

Use `pip install 'spectraccess[cdse]'`. The processor version and baseline
collection are preserved when CDSE publishes them. Collection identity is
read from `baselineCollection`, not guessed from a current release number.
Files are decoded using their own scale factors, units and fill values.

The default parser excludes INVALID, CLOUD, CLOUD_AMBIGUOUS, CLOUD_MARGIN,
SNOW_ICE, SATURATED, SUSPECT and WVFAIL pixels. `include_flagged=True` preserves
these finite values with `qa.accepted=False`. Fill values are always omitted.
Full flag definitions and active names accompany each row.

`IWV_err` is carried as `unc_value` with `unc_k=1`. `unc_definition` quotes
the product format specification: "Uncertainty estimate for the Integrated
water vapour column above the current pixel" (section 4.2.1.1; issue 2.4
names the variable `IWV_unc`). Values are in kg m-2.
The cited cloud-free land validation found a positive wet bias of 7 to 10
percent. `published_bias` retains that range, reference and scope with
`applied=False`; observations are never corrected using that range.

OL_2_LFR does not publish a retrieval prior state/covariance or averaging
kernel in these assets. These fields remain absent. Pixel centre and elevation
are retained when provided; no pixel-boundary polygon or integration duration
is inferred from nominal 300 m spacing or scene duration.

Usage and connector release notes: [atmospheric connectors](../../../../docs/atmospheric-connectors.md).
