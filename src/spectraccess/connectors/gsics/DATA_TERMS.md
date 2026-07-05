# GSICS GPPA Data Terms

The spectrAccess GSICS connector is a fetch mechanism for public GSICS GPPA
products exposed by agency-operated portals. spectrAccess does not redistribute
GSICS source data.

Users are responsible for complying with the terms and attribution requirements
of the relevant GSICS and agency source portals, including EUMETSAT, NOAA STAR,
and CMA where applicable.

## Verified live catalogs (2026-07-05)

- EUMETSAT: `https://gsics.eumetsat.int/thredds/catalog.xml`
- CMA: `https://gsics.nsmc.org.cn/thredds/catalog.xml`
- NOAA STAR: documented canonical URL `https://www.star.nesdis.noaa.gov/thredds/gsics/catalog.xml`,
  but the host is unreachable as of 2026-07-05 (connection-level failure from
  both EU and US vantage points, likely WAF/outage). The connector keeps this
  catalog disabled (`url=None`) by default until reachability is reconfirmed.
  NESDIS product families are also mirrored on the EUMETSAT GSICS
  collaboration server's master catalog (`nesdisProducts.xml`).

## Confirmed rights statement (from a downloaded EUMETSAT GSICS product file)

> Information delivered as a GSICS product is generated in accordance with the
> GSICS principles and practices. GSICS products are public and may be used
> and redistributed freely. Any publication using GSICS products should
> acknowledge both GSICS and the relevant data creator organization. Neither
> the data creator, nor the data publisher, nor any of their employees or
> contractors, makes any warranty, express or implied, including warranties of
> merchantability and fitness for a particular purpose, or assumes any legal
> liability for the accuracy, completeness, or usefulness, of this
> information.

The EUMETSAT THREDDS catalog documents this product family with
`<documentation type="Rights">Freely available</documentation>`.

