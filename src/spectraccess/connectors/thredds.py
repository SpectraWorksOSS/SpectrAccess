from __future__ import annotations

from collections import deque
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


@dataclass(frozen=True)
class ThreddsCatalogRef:
    name: str
    href: str
    catalog_url: str


def _service_type(attrib: dict) -> str | None:
    return attrib.get("serviceType")


def _find_service_base(service_elem: ElementTree.Element, want_name: str | None) -> str | None:
    """Search a (possibly Compound) service element tree for an HTTPServer base.

    If `want_name` is given, only match a service whose name equals it (searching
    into Compound children). If `want_name` is None, return the first HTTPServer
    base found anywhere in the tree.
    """
    name = service_elem.attrib.get("name")
    service_type = _service_type(service_elem.attrib)
    if want_name is not None:
        if name == want_name:
            if service_type == "HTTPServer":
                return service_elem.attrib.get("base")
            # Compound service matched by name: search its children for HTTPServer.
            for child in service_elem.findall("t:service", THREDDS_NS):
                base = _find_service_base(child, None)
                if base is not None:
                    return base
            return None
        # Not this one by name; still recurse into compound children in case the
        # wanted name lives nested under a different top-level compound name.
        for child in service_elem.findall("t:service", THREDDS_NS):
            base = _find_service_base(child, want_name)
            if base is not None:
                return base
        return None
    # No specific name wanted: first HTTPServer anywhere.
    if service_type == "HTTPServer":
        return service_elem.attrib.get("base")
    for child in service_elem.findall("t:service", THREDDS_NS):
        base = _find_service_base(child, None)
        if base is not None:
            return base
    return None


def _resolve_http_base(root: ElementTree.Element, service_name: str | None) -> str | None:
    top_services = root.findall("t:service", THREDDS_NS)
    for service in top_services:
        base = _find_service_base(service, service_name)
        if base is not None:
            return base
    if service_name is not None:
        # Fall back to any HTTPServer regardless of name if the named one wasn't found.
        for service in top_services:
            base = _find_service_base(service, None)
            if base is not None:
                return base
    return None


def _inherited_service_name(root: ElementTree.Element) -> str | None:
    metadata = root.find("t:metadata", THREDDS_NS)
    if metadata is not None:
        service_name_elem = metadata.find("t:serviceName", THREDDS_NS)
        if service_name_elem is not None and service_name_elem.text:
            return service_name_elem.text.strip()
    return None


def _access_url_for_dataset(
    root: ElementTree.Element,
    dataset: ElementTree.Element,
    catalog_url: str,
    url_path: str,
    inherited_service_name: str | None,
) -> tuple[str | None, str | None]:
    access = dataset.find("t:access", THREDDS_NS)
    if access is not None:
        service_name = access.attrib.get("serviceName")
        base = _resolve_http_base(root, service_name)
        if base is not None:
            return urljoin(catalog_url, base) + url_path, service_name
    # No explicit <access>: fall back to inherited/declared service.
    base = _resolve_http_base(root, inherited_service_name)
    if base is not None:
        return urljoin(catalog_url, base) + url_path, inherited_service_name
    return None, None


def parse_catalog(xml: bytes | str, catalog_url: str, source_agency: str | None = None) -> list[ThreddsDataset]:
    root = ElementTree.fromstring(xml)
    inherited_service_name = _inherited_service_name(root)
    datasets: list[ThreddsDataset] = []
    for dataset in root.findall(".//t:dataset", THREDDS_NS):
        url_path = dataset.attrib.get("urlPath")
        if not url_path:
            continue
        name = dataset.attrib.get("name", url_path)
        access_url, service_name = _access_url_for_dataset(
            root, dataset, catalog_url, url_path, inherited_service_name
        )
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


def list_catalog_refs(xml: bytes | str, catalog_url: str) -> list[ThreddsCatalogRef]:
    root = ElementTree.fromstring(xml)
    refs: list[ThreddsCatalogRef] = []
    for ref in root.findall(".//t:catalogRef", THREDDS_NS):
        href = ref.attrib.get("{http://www.w3.org/1999/xlink}href")
        if not href:
            continue
        title = ref.attrib.get("{http://www.w3.org/1999/xlink}title") or ref.attrib.get("name") or href
        resolved = urljoin(catalog_url, href)
        refs.append(ThreddsCatalogRef(name=title, href=href, catalog_url=resolved))
    return refs


def fetch_catalog(catalog_url: str, *, source_agency: str | None = None, **fetch_kwargs: object) -> list[ThreddsDataset]:
    return parse_catalog(fetch_url(catalog_url, **fetch_kwargs), catalog_url, source_agency)


def walk_catalog(
    catalog_url: str,
    *,
    max_depth: int = 4,
    max_catalogs: int = 50,
    ref_filter=None,
    source_agency: str | None = None,
    **fetch_kwargs: object,
) -> list[ThreddsDataset]:
    """Breadth-first walk of a THREDDS catalog tree, following catalogRefs.

    Collects ThreddsDataset leaves (datasets with a urlPath). Guards against
    revisiting the same catalog URL and stops once `max_catalogs` catalogs have
    been visited or a branch exceeds `max_depth` levels from the root.
    `ref_filter(ThreddsCatalogRef) -> bool` optionally prunes which catalogRefs
    get followed.
    """
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(catalog_url, 0)])
    datasets: list[ThreddsDataset] = []

    while queue and len(visited) < max_catalogs:
        url, depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        xml = fetch_url(url, **fetch_kwargs)
        datasets.extend(parse_catalog(xml, url, source_agency=source_agency))

        if depth >= max_depth:
            continue

        for ref in list_catalog_refs(xml, url):
            if ref.catalog_url in visited:
                continue
            if ref_filter is not None and not ref_filter(ref):
                continue
            if len(visited) + len(queue) >= max_catalogs:
                break
            queue.append((ref.catalog_url, depth + 1))

    return datasets
