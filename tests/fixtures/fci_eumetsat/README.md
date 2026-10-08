# Synthetic FCI fixtures

`generate.py` creates small native-grid NetCDF files following the FCI L1c
PUG Appendices A.2/A.3 and L2 cloud-product layout. Effective radiance is
losslessly compressed with FCIDECOMP (JPEG-LS, HDF5 filter 32018), as specified
in the [FCI L1c data guide](https://user.eumetsat.int/resources/user-guides/mtg-fci-level-1c-data-guide).
Values, times and product identifiers are artificial; no observation is downloaded.

`search.json` models the Data Store OpenSearch FeatureCollection: `date` is
a sensing interval, `updated` is publication time, and `links.sip-entries`
contains entry titles and download links. `metadata.json` models the download
metadata response. `osdd.xml` supplies the OpenSearch parameter description.
These shapes match EUMDAC 3.1.1's Collection/SearchResults, Product and DataStore
parsers and the [public browse API](https://api.eumetsat.int/data/browse/1.0.0/collections/EO%3AEUM%3ADAT%3A0662).
The test HTTP adapter serves these documents and generated NetCDF bytes;
EUMDAC itself builds and parses every catalogue, token and entry request.
