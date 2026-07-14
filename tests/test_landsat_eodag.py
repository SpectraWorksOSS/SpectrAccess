from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from eodag.api.product import EOProduct

from spectraccess.connectors.landsat_eodag import (
    LandsatDownloadError,
    LandsatEodagConnector,
    LandsatProductError,
    target_to_canonical,
)


L8_TITLE = "LC08_L1TP_044034_20210508_20210518_02_T1"


def _product(*, title: str = L8_TITLE, cloud_cover: object = 12.5) -> EOProduct:
    return EOProduct(
        "usgs",
        {
            "id": "LC80440342021128LGN00",
            "title": title,
            "start_datetime": "2021-05-08T18:06:42Z",
            "end_datetime": "2021-05-08T18:07:13Z",
            "eo:cloud_cover": cloud_cover,
            "size": 1_234_567_890,
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-123.0, 37.0], [-122.0, 37.0], [-122.0, 38.0], [-123.0, 37.0]]],
            },
            "eodag:download_link": "https://provider.example/archive",
        },
        productType="LANDSAT_C2L1",
    )


class Gateway:
    def __init__(self, products=()):
        self.products = list(products)
        self.search_calls = []
        self.download_calls = []
        self.config_updates = []
        self.preferred = []

    def update_providers_config(self, *, dict_conf):
        self.config_updates.append(dict_conf)

    def set_preferred_provider(self, provider):
        self.preferred.append(provider)

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return self.products

    def download(self, raw, **kwargs):
        self.download_calls.append((raw, kwargs))
        return None


def test_discover_preserves_refcal_identity_and_source_cache_contract():
    gateway = Gateway([_product()])
    target = LandsatEodagConnector(gateway=gateway).discover(
        bbox=(-123.0, 37.0, -122.0, 38.0),
        start=datetime(2021, 5, 8),
        end=datetime(2021, 5, 8),
        max_cloud_cover=20,
        limit=5,
    )[0]

    assert gateway.search_calls == [
        {
            "provider": "usgs",
            "collection": "LANDSAT_C2L1",
            "limit": 5,
            "raise_errors": True,
            "geom": {"lonmin": -123.0, "latmin": 37.0, "lonmax": -122.0, "latmax": 38.0},
            "start": "2021-05-08",
            "end": "2021-05-09",
        }
    ]
    assert target.product_id == "LC80440342021128LGN00"
    assert target.title == L8_TITLE
    assert target.sensor_id == "L8_OLI"
    assert target.platform_id == "LC08"
    assert target.product_level == "L1TP"
    assert target.collection_number == "02"
    assert target.tier == "T1"
    assert target.product_type == "LANDSAT_C2_L1TP_T1"
    assert target.source_family == "landsat_c2_l1tp"
    assert (target.wrs_path, target.wrs_row) == (44, 34)
    assert target.cache_identifier == "usgs:LC80440342021128LGN00"
    assert target.size_mb == pytest.approx(1234.56789)
    assert target.raw is gateway.products[0]
    assert target.raw_metadata["eodag:download_link"] == "https://provider.example/archive"


@pytest.mark.parametrize(
    "title,tier,product_type,sensor",
    [
        (L8_TITLE, "T1", "LANDSAT_C2_L1TP_T1", "L8_OLI"),
        ("LC09_L1TP_044034_20220508_20220510_02_T2", "T2", "LANDSAT_C2_L1TP_T2", "L9_OLI2"),
        ("LC09_L1TP_044034_20220508_20220510_02_RT", "RT", "LANDSAT_C2_L1TP_RT", "L9_OLI2"),
    ],
)
def test_collection_two_tier_semantics_are_explicit(title, tier, product_type, sensor):
    target = LandsatEodagConnector(gateway=Gateway([_product(title=title)])).discover(
        bbox=(-1, -1, 1, 1)
    )[0]
    assert (target.tier, target.product_type, target.sensor_id) == (tier, product_type, sensor)


def test_discover_title_strips_archive_suffix_and_requests_exact_display_id():
    gateway = Gateway([_product()])
    targets = LandsatEodagConnector(gateway=gateway).discover_title(f"{L8_TITLE}.tar.gz", limit=3)
    assert targets[0].title == L8_TITLE
    assert gateway.search_calls == [
        {
            "provider": "usgs",
            "collection": "LANDSAT_C2L1",
            "id": L8_TITLE,
            "limit": 3,
            "raise_errors": True,
        }
    ]


def test_constructor_passes_byo_credentials_only_to_eodag_config():
    gateway = Gateway()
    connector = LandsatEodagConnector(
        gateway=gateway,
        username="usgs-user",
        password="m2m-token",
    )
    assert connector.provider == "usgs"
    assert gateway.preferred == ["usgs"]
    assert gateway.config_updates == [
        {
            "usgs": {
                "products": {"LANDSAT_C2L1": {"_collection": "landsat_ot_c2_l1"}},
                "api": {
                    "type": "UsgsApi",
                    "credentials": {"username": "usgs-user", "password": "m2m-token"},
                },
            }
        }
    ]
    assert "usgs-user" not in repr(connector)


def test_fetch_delegates_wait_timeout_and_finds_only_matching_archive(tmp_path):
    gateway = Gateway([_product()])
    connector = LandsatEodagConnector(gateway=gateway)
    target = connector.discover(bbox=(-1, -1, 1, 1))[0]
    (tmp_path / "unrelated.tar.gz").write_bytes(b"wrong")
    expected = tmp_path / f"{L8_TITLE}.tar.gz"
    expected.write_bytes(b"archive")

    result = connector.fetch(target, dest=tmp_path, wait=2.0, timeout=30.0)

    assert result == str(expected)
    assert gateway.download_calls == [
        (
            target.raw,
            {"output_dir": str(tmp_path), "extract": False, "wait": 2.0, "timeout": 30.0},
        )
    ]


def test_fetch_fails_when_eodag_reports_no_matching_archive(tmp_path):
    connector = LandsatEodagConnector(gateway=Gateway([_product()]))
    target = connector.discover(bbox=(-1, -1, 1, 1))[0]
    (tmp_path / "unrelated.tar").write_bytes(b"wrong")
    with pytest.raises(LandsatDownloadError, match="no matching archive"):
        connector.fetch(target, dest=tmp_path)


def test_native_and_canonical_metadata_are_lossless_and_honest():
    connector = LandsatEodagConnector(gateway=Gateway([_product()]))
    target = connector.discover(bbox=(-1, -1, 1, 1))[0]
    native = connector.parse("/cloud/source-cache/archive.tar.gz", target=target)
    assert native.loc[0, "product_type"] == "LANDSAT_C2_L1TP_T1"
    assert native.loc[0, "cache_identifier"] == "usgs:LC80440342021128LGN00"
    assert native.attrs["source_metadata"]["title"] == L8_TITLE

    canonical = target_to_canonical(target, local_path="/cloud/source-cache/archive.tar.gz")
    assert canonical.attrs["spectraccess_schema_version"] == "1.0"
    assert canonical.loc[0, "platform"] == "L8_OLI"
    assert canonical.loc[0, "instrument"] == "OLI"
    assert canonical.loc[0, "quantity"] == "scene_cloud_cover"
    assert canonical.loc[0, "value"] == pytest.approx(12.5)
    assert canonical.loc[0, "unc_status"] == "unknown"
    assert pd.isna(canonical.loc[0, "unc_value"])
    assert canonical.loc[0, "product_type"] == "LANDSAT_C2_L1TP_T1"


@pytest.mark.parametrize(
    "title",
    [
        "LC08_L1GT_044034_20210508_20210518_02_T1",
        "LC08_L1TP_044034_20210508_20210518_01_T1",
        "LE07_L1TP_044034_20210508_20210518_02_T1",
    ],
)
def test_non_contract_landsat_titles_fail_loudly(title):
    connector = LandsatEodagConnector(gateway=Gateway([_product(title=title)]))
    with pytest.raises(LandsatProductError):
        connector.discover(bbox=(-1, -1, 1, 1))


def test_unknown_cloud_cover_is_not_fabricated_or_admitted_by_max_filter():
    connector = LandsatEodagConnector(gateway=Gateway([_product(cloud_cover=None)]))
    assert connector.discover(bbox=(-1, -1, 1, 1))[0].cloud_cover is None
    assert connector.discover(bbox=(-1, -1, 1, 1), max_cloud_cover=100) == []
