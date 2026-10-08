# NGL GNSS troposphere access and data terms

Code is Apache-2.0. Nevada Geodetic Laboratory source products are not
relicensed by spectrAccess. NGL publishes the station metadata and troposphere
archives without authentication. Public download access alone does not establish
a blanket redistribution or commercial-use licence. The product README does
not specify one; confirm intended use with NGL where explicit permission is
needed. spectrAccess retrieves on your behalf and does not re-serve archives.

- NGL source, publications and contact: https://geodesy.unr.edu/
- Product README: https://geodesy.unr.edu/gps_timeseries/readmes/README_trop2.txt
- Current IGS20 archive: https://geodesy.unr.edu/gps_timeseries/IGS20/trop/
- Station coordinates: https://geodesy.unr.edu/NGLStationPages/llh.out
- Processing context: https://doi.org/10.1029/2018EO104623

Acknowledge NGL and the underlying station operators and cite the relevant
NGL processing publications for scientific use. Follow any additional terms
of the underlying GNSS networks. No credentials are required.

Archives contain daily gzip SINEX files inside station/year ZIPs. Fetching one
day retrieves its annual ZIP, bounded by `max_bytes` (10 MB by default).
`NGLResult` carries the exact archive URL, station and retrieval time. Station
`height_m` is ellipsoidal height in metres from NGL's llh table. Parsing a
standalone file without that table uses the approximate SINEX SITE/ID location
and labels the location basis in QA. Processing software and reference frame
are read from the file, not assumed to be a fixed GipsyX release.

Rows contain `zenith_total_delay` and available `tropospheric_gradient_north`
and `tropospheric_gradient_east`, in metres. No conversion to water vapour is
performed. The README calls the reported sigmas "formal error columns (_SIG)";
`unc_definition` closely paraphrases this as "Formal error columns (_SIG) for
the tropospheric estimates". These errors are `provided`; the source does not publish a coverage factor,
so `unc_k` is null. The
five-minute interval is sampling cadence, retained in QA.

The README warns that TGETOT/TGNTOT columns and their formal errors are
interchanged. The parser corrects both directions by default, retains all
reported fields, and declares `qa.gradient_columns_interchanged=True`.
Use `correct_gradient_swap=False` to preserve the header directions for a
provider file whose formatting has been corrected. Fill values are omitted;
missing/negative formal errors produce `unc_status='unknown'`.

No water-vapour prior or observation averaging kernel is published for these
ZTD estimates. These metadata remain absent. VMF1/NWM inputs described in the
file remain visible in the reported processing provenance.

Usage and connector release notes: [atmospheric connectors](../../../../docs/atmospheric-connectors.md).
