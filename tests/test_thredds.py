from __future__ import annotations

from spectraccess.connectors.thredds import parse_catalog, walk_catalog


ROOT_CATALOG = """<?xml version="1.0" encoding="UTF-8"?>
<catalog xmlns="http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"
         xmlns:xlink="http://www.w3.org/1999/xlink"
         name="Root Master Catalog">
  <dataset name="GSICS Products">
    <catalogRef xlink:href="productsA.xml" xlink:title="Product family A" name="Product family A"/>
    <catalogRef xlink:href="/thredds/catalog/product-b/catalog.xml" xlink:title="Product family B"/>
  </dataset>
</catalog>
"""

PRODUCTS_A_CATALOG = """<?xml version="1.0" encoding="UTF-8"?>
<catalog xmlns="http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"
         xmlns:xlink="http://www.w3.org/1999/xlink"
         name="Product family A catalog">
  <service name="allServices" serviceType="Compound" base="">
    <service name="HTTPServer" serviceType="HTTPServer" base="/thredds/fileServer/"/>
    <service name="OpenDAP" serviceType="OpenDAP" base="/thredds/dodsC/"/>
  </service>
  <metadata inherited="true">
    <serviceName>allServices</serviceName>
    <dataFormat>netCDF</dataFormat>
  </metadata>
  <dataset name="leaf.nc" urlPath="product-a/leaf.nc"/>
  <catalogRef xlink:href="productsA.xml" xlink:title="Product family A"/>
</catalog>
"""

PRODUCT_B_CATALOG = """<?xml version="1.0" encoding="UTF-8"?>
<catalog xmlns="http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"
         xmlns:xlink="http://www.w3.org/1999/xlink"
         name="Product family B catalog">
  <service name="allServices" serviceType="Compound" base="">
    <service name="HTTPServer" serviceType="HTTPServer" base="/thredds/fileServer/"/>
  </service>
  <metadata inherited="true">
    <serviceName>allServices</serviceName>
  </metadata>
  <dataset name="other-leaf.nc" urlPath="product-b/other-leaf.nc"/>
</catalog>
"""


def _register(requests_mock, url: str, text: str) -> None:
    requests_mock.get(url, text=text)


def test_walk_catalog_resolves_compound_service_and_relative_ref(requests_mock, tmp_path):
    root_url = "https://gsics.example.test/thredds/catalog.xml"
    products_a_url = "https://gsics.example.test/thredds/productsA.xml"
    product_b_url = "https://gsics.example.test/thredds/catalog/product-b/catalog.xml"

    _register(requests_mock, root_url, ROOT_CATALOG)
    _register(requests_mock, products_a_url, PRODUCTS_A_CATALOG)
    _register(requests_mock, product_b_url, PRODUCT_B_CATALOG)

    datasets = walk_catalog(
        root_url,
        max_depth=4,
        max_catalogs=10,
        source_agency="TEST",
        cache_dir=tmp_path,
        use_cache=False,
    )

    by_name = {d.name: d for d in datasets}
    assert "leaf.nc" in by_name
    assert "other-leaf.nc" in by_name

    leaf = by_name["leaf.nc"]
    assert leaf.access_url == "https://gsics.example.test/thredds/fileServer/product-a/leaf.nc"
    assert leaf.source_agency == "TEST"

    other_leaf = by_name["other-leaf.nc"]
    # Absolute-path catalogRef (/thredds/catalog/product-b/catalog.xml) must
    # resolve against the root catalog's host, not be treated as relative.
    assert other_leaf.access_url == "https://gsics.example.test/thredds/fileServer/product-b/other-leaf.nc"

    # Cycle guard: productsA.xml catalogRefs back to itself; it must be fetched
    # exactly once (not infinitely re-queued).
    assert requests_mock.call_count == 3


def test_walk_catalog_ref_filter_prunes_branches(requests_mock, tmp_path):
    root_url = "https://gsics.example.test/thredds/catalog.xml"
    products_a_url = "https://gsics.example.test/thredds/productsA.xml"
    product_b_url = "https://gsics.example.test/thredds/catalog/product-b/catalog.xml"

    _register(requests_mock, root_url, ROOT_CATALOG)
    _register(requests_mock, products_a_url, PRODUCTS_A_CATALOG)
    _register(requests_mock, product_b_url, PRODUCT_B_CATALOG)

    datasets = walk_catalog(
        root_url,
        max_depth=4,
        max_catalogs=10,
        ref_filter=lambda ref: "A" in ref.name,
        cache_dir=tmp_path,
        use_cache=False,
    )

    names = {d.name for d in datasets}
    assert "leaf.nc" in names
    assert "other-leaf.nc" not in names
    # product_b_url should never have been fetched since its ref was filtered out.
    assert not any(req.url == product_b_url for req in requests_mock.request_history)


def test_walk_catalog_respects_max_catalogs(requests_mock, tmp_path):
    root_url = "https://gsics.example.test/thredds/catalog.xml"
    products_a_url = "https://gsics.example.test/thredds/productsA.xml"
    product_b_url = "https://gsics.example.test/thredds/catalog/product-b/catalog.xml"

    _register(requests_mock, root_url, ROOT_CATALOG)
    _register(requests_mock, products_a_url, PRODUCTS_A_CATALOG)
    _register(requests_mock, product_b_url, PRODUCT_B_CATALOG)

    walk_catalog(
        root_url,
        max_depth=4,
        max_catalogs=1,
        cache_dir=tmp_path,
        use_cache=False,
    )

    # Only the root catalog itself should be fetched when max_catalogs=1.
    assert requests_mock.call_count == 1


def test_parse_catalog_access_url_for_urlpath_leaf():
    catalog_url = "https://gsics.example.test/thredds/productsA.xml"
    datasets = parse_catalog(PRODUCTS_A_CATALOG, catalog_url, source_agency="TEST")

    leaf = next(d for d in datasets if d.name == "leaf.nc")
    assert leaf.access_url == "https://gsics.example.test/thredds/fileServer/product-a/leaf.nc"
