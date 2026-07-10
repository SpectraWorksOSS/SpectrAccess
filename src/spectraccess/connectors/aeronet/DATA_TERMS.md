# AERONET Data Terms

## Data policy

NASA AERONET data are publicly and freely available for research. Users must
acknowledge AERONET and the specific site principal investigator(s) in
publications, in accordance with the AERONET data-use policy. Users remain
responsible for complying with that policy and consulting the current AERONET
site and data documentation when publishing results.

## Account and credentials

AERONET's v3 web service is public and requires no account or credentials.
spectrAccess is bring-your-own-nothing: the connector accesses the public
service directly and ships no secrets.

## Test-fixture data and licensing

The package includes two trimmed real AERONET L2.0 CSV responses solely for
testing the parser: Granada (PI Lucas Alados-Arboledas) and Ispra (PI Barbara
Bulgarelli). These files are **data**, redistributed under the AERONET policy
with acknowledgment; they are not covered by this repository's Apache-2.0
software licence. The Apache-2.0 licence applies to the connector code, not to
the AERONET observations.
