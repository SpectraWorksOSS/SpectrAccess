from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin
from xml.etree import ElementTree

from spectraccess.core.fetch import fetch_url


THREDDS_NS = {"t": "http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"}


@dataclass(frozen=True)
class ThreddsDataset:
    name: str
    catalog_url: str
    access_url: str | None = None
    service: str | None = None
    source_agency: str | None = None


def parse_catalog(xml: bytes | str, catalog_url: str, source_agency: str | None = None) -> list[ThreddsDataset]:
    root = ElementTree.fromstring(xml)
    services = {
        service.attrib.get("name"): service.attrib
        for service in root.findall(".//t:service", THREDDS_NS)
    }
    datasets: list[ThreddsDataset] = []
    for dataset in root.findall(".//t:dataset", THREDDS_NS):
        url_path = dataset.attrib.get("urlPath")
        if not url_path:
            continue
        name = dataset.attrib.get("name", url_path)
        access = dataset.find("t:access", THREDDS_NS)
        service_name = access.attrib.get("serviceName") if access is not None else None
        service = services.get(service_name or "")
        base = service.get("base") if service else None
        access_url = urljoin(catalog_url, base + url_path) if base else None
        datasets.append(
            ThreddsDataset(
                name=name,
                catalog_url=catalog_url,
                access_url=access_url,
                service=service_name,
                source_agency=source_agency,
            )
        )
    return datasets


def fetch_catalog(catalog_url: str, *, source_agency: str | None = None, **fetch_kwargs: object) -> list[ThreddsDataset]:
    return parse_catalog(fetch_url(catalog_url, **fetch_kwargs), catalog_url, source_agency)

