"""EMIT product discovery and download through NASA's maintained earthaccess.

This is a source-access adapter, not an EMIT science processor. earthaccess
remains authoritative for CMR queries, Earthdata authentication, and transfer.
spectrAccess adds a stable target, explicit failures, checksum verification,
and canonical metadata provenance. It never loads or reshapes EMIT cubes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import pandas as pd

from spectraccess.core.connector import Connector
from spectraccess.core.schema import Uncertainty, UncertaintyStatus, uncertainty_columns, validate

try:
    import earthaccess
except ImportError as exc:  # pragma: no cover - packaging boundary
    raise ImportError(
        "EMITEarthaccessConnector requires NASA's maintained earthaccess client. "
        "Install it with: pip install 'spectraccess[emit]'"
    ) from exc


SUPPORTED_COLLECTIONS: Mapping[str, str] = {
    "EMITL1BRAD": "001",
    "EMITL2ARFL": "001",
}
SOURCE = "nasa-earthdata-lp-daac"
SOURCE_AGENCY = "NASA / LP DAAC"
_DATA_HOST = "data.lpdaac.earthdatacloud.nasa.gov"
_MAX_RESULTS = 2_000

try:
    EARTHACCESS_VERSION = version("earthaccess")
except PackageNotFoundError:  # pragma: no cover
    EARTHACCESS_VERSION = "unknown"


class EMITConnectorError(RuntimeError):
    """Base error at the public EMIT connector boundary."""


class EMITProviderError(EMITConnectorError):
    """earthaccess/CMR failed rather than returning a clean empty result."""


class EMITDownloadError(EMITConnectorError):
    """Earthdata transfer or checksum verification failed."""


class EMITProductError(EMITConnectorError):
    """CMR metadata is absent, malformed, or contradicts the supported product."""


@dataclass(frozen=True)
class EMITTarget:
    """Lossless public record crossing the EMIT discover -> fetch boundary."""

    product_id: str
    title: str
    collection: str
    version: str
    concept_id: str
    collection_concept_id: str
    provider_id: str
    platform_id: str
    sensor_id: str
    start_time: datetime
    end_time: datetime
    footprint_wkt: str
    cloud_cover: float | None
    orbit: str | None
    orbit_segment: str | None
    scene: str | None
    solar_zenith_deg: float | None
    solar_azimuth_deg: float | None
    size_mb: float | None
    assets: Mapping[str, str]
    asset_sizes_bytes: Mapping[str, int]
    checksums: Mapping[str, tuple[str, str]]
    catalogue_url: str
    retrieved_at: datetime
    provider: str = "earthdata-cmr"
    provider_client: str = "earthaccess"
    provider_client_version: str = EARTHACCESS_VERSION
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


class EMITEarthaccessConnector(Connector):
    """Thin public adapter for official EMIT L1B radiance and L2A reflectance.

    Discovery is public. Fetch uses earthaccess's BYO Earthdata credential
    chain at call time. Connector availability is not claim-grade admission;
    cube, GLT, wavelength-grid, mask, and science policy stay downstream.
    """

    def discover(
        self,
        *,
        product: str = "EMITL2ARFL",
        version: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        granule_name: str | None = None,
        limit: int = 10,
        **_kwargs: object,
    ) -> list[EMITTarget]:
        """Return bounded official CMR granules, newest first."""
        product = product.upper()
        if product not in SUPPORTED_COLLECTIONS:
            raise ValueError(
                f"unsupported EMIT product {product!r}; supported: {sorted(SUPPORTED_COLLECTIONS)}"
            )
        expected_version = SUPPORTED_COLLECTIONS[product]
        selected_version = version or expected_version
        if selected_version != expected_version:
            raise ValueError(
                f"unsupported {product} version {selected_version!r}; "
                f"reviewed version is {expected_version!r}"
            )
        if limit < 0 or limit > _MAX_RESULTS:
            raise ValueError(f"limit must be between 0 and {_MAX_RESULTS}")
        if limit == 0:
            return []
        if bbox is not None:
            _validate_bbox(bbox)

        start_utc = _as_start(start)
        end_utc = _as_end(end)
        if start_utc is not None and end_utc is not None and end_utc < start_utc:
            raise ValueError(f"end must be >= start (got start={start_utc}, end={end_utc})")

        query: dict[str, object] = {
            "short_name": product,
            "version": selected_version,
            "count": limit,
        }
        if bbox is not None:
            query["bounding_box"] = bbox
        if start_utc is not None or end_utc is not None:
            query["temporal"] = (
                start_utc or datetime(2022, 8, 9, tzinfo=timezone.utc),
                end_utc or datetime.now(timezone.utc),
            )
        if granule_name is not None:
            query["granule_name"] = granule_name

        try:
            granules = list(earthaccess.search_data(**query))
        except Exception as exc:
            raise EMITProviderError(f"EMIT CMR discovery failed: {exc}") from exc
        targets = [_target_from_granule(granule, expected_product=product) for granule in granules]
        return sorted(targets, key=lambda target: target.start_time, reverse=True)

    def fetch(
        self,
        target: EMITTarget,
        *,
        dest: str | Path,
        asset: str = "primary",
        login_strategy: str = "environment",
        threads: int = 1,
        verify_checksum: bool = True,
        **_kwargs: object,
    ) -> str:
        """Download exactly one selected NetCDF asset through earthaccess.

        ``asset`` accepts ``primary``, ``uncertainty``, ``mask``,
        ``observation``, or an exact filename. One explicit file prevents
        accidental whole-granule transfer.
        """
        filename, url = _select_asset(target, asset)
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https" or parsed_url.hostname != _DATA_HOST:
            raise EMITDownloadError(
                f"refusing to send Earthdata credentials to off-origin asset URL {url!r}"
            )
        if threads < 1:
            raise ValueError("threads must be >= 1")
        output_dir = Path(dest)
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            earthaccess.login(strategy=login_strategy)
            outputs = earthaccess.download(
                [url], local_path=output_dir, threads=threads, show_progress=False
            )
        except Exception as exc:
            raise EMITDownloadError(f"Earthdata download failed for {filename}: {exc}") from exc
        paths = [Path(path) for path in (outputs or [])]
        if len(paths) != 1:
            raise EMITDownloadError(
                f"earthaccess returned {len(paths)} output path(s) for one requested asset {filename!r}"
            )
        output_path = paths[0]
        if not output_path.exists() or not output_path.is_file():
            raise EMITDownloadError(
                f"earthaccess reported {output_path}, but the downloaded asset does not exist"
            )
        if output_path.name != filename:
            raise EMITDownloadError(
                f"earthaccess returned {output_path.name!r}, expected CMR asset {filename!r}"
            )
        if verify_checksum:
            _verify_checksum(output_path, target)
        return str(output_path)

    def parse(self, raw: bytes | str, *, target: EMITTarget | None = None) -> pd.DataFrame:
        """Return one product-metadata row; EMIT pixels stay untouched."""
        if target is None:
            raise EMITProductError(
                "EMIT metadata parsing requires the discovered target so provenance is not guessed; "
                "call parse(path, target=target) or Connector.run()"
            )
        return target_to_frame(target, local_path=_local_path(raw))

    def parse_canonical(
        self,
        raw: bytes | str,
        *,
        target: EMITTarget | None = None,
        retrieved_at: datetime | None = None,
    ) -> pd.DataFrame:
        """Emit source metadata quantities with honest unknown uncertainty."""
        if target is None:
            raise EMITProductError(
                "canonical EMIT metadata requires the discovered target; "
                "call parse_canonical(path, target=target) or Connector.run(canonical=True)"
            )
        return target_to_canonical(target, local_path=_local_path(raw), retrieved_at=retrieved_at)

    def _parse_kwargs_for(self, target: object) -> dict[str, object]:
        return {"target": target} if isinstance(target, EMITTarget) else {}

    def _canonical_kwargs_for(self, target: object) -> dict[str, object]:
        return {"target": target} if isinstance(target, EMITTarget) else {}


def target_to_frame(target: EMITTarget, *, local_path: str | None = None) -> pd.DataFrame:
    """Represent the stable CMR product record without interpreting pixels."""
    return pd.DataFrame([{
        "product_id": target.product_id,
        "title": target.title,
        "collection": target.collection,
        "version": target.version,
        "concept_id": target.concept_id,
        "collection_concept_id": target.collection_concept_id,
        "provider_id": target.provider_id,
        "platform": target.platform_id,
        "sensor": target.sensor_id,
        "start_time": target.start_time,
        "end_time": target.end_time,
        "footprint_wkt": target.footprint_wkt,
        "cloud_cover": target.cloud_cover,
        "orbit": target.orbit,
        "orbit_segment": target.orbit_segment,
        "scene": target.scene,
        "solar_zenith_deg": target.solar_zenith_deg,
        "solar_azimuth_deg": target.solar_azimuth_deg,
        "size_mb": target.size_mb,
        "assets": dict(target.assets),
        "asset_sizes_bytes": dict(target.asset_sizes_bytes),
        "checksums": dict(target.checksums),
        "catalogue_url": target.catalogue_url,
        "provider": target.provider,
        "provider_client": target.provider_client,
        "provider_client_version": target.provider_client_version,
        "retrieved_at": target.retrieved_at,
        "local_path": local_path,
    }])


def target_to_canonical(
    target: EMITTarget,
    *,
    local_path: str | None = None,
    retrieved_at: datetime | None = None,
) -> pd.DataFrame:
    """Canonicalize CMR scene metadata, not EMIT radiance/reflectance pixels."""
    latitude, longitude = _footprint_centroid(target.footprint_wkt)
    quantities = [
        ("scene_cloud_cover", target.cloud_cover, "percent"),
        ("solar_zenith_angle", target.solar_zenith_deg, "degree"),
        ("solar_azimuth_angle", target.solar_azimuth_deg, "degree"),
    ]
    rows: list[dict[str, object]] = []
    unknown = uncertainty_columns(Uncertainty(None, UncertaintyStatus.UNKNOWN))
    for quantity, value, units in quantities:
        if value is None:
            continue
        rows.append({
            "time": target.start_time,
            "platform": target.platform_id,
            "instrument": target.sensor_id,
            "band": None,
            "wavelength_nm": None,
            "site": None,
            "latitude": latitude,
            "longitude": longitude,
            "reference": None,
            "quantity": quantity,
            "value": value,
            "units": units,
            **unknown,
            "source": SOURCE,
            "source_agency": SOURCE_AGENCY,
            "source_url": target.catalogue_url,
            "retrieved_at": retrieved_at or target.retrieved_at,
            "product_id": target.product_id,
            "collection": target.collection,
            "collection_version": target.version,
            "orbit": target.orbit,
            "scene": target.scene,
            "footprint_wkt": target.footprint_wkt,
            "local_path": local_path,
        })
    if not rows:
        raise EMITProductError(
            f"CMR target {target.product_id!r} has no supported numerical metadata to canonicalize"
        )
    return validate(pd.DataFrame(rows))


def _target_from_granule(granule: Mapping[str, Any], *, expected_product: str) -> EMITTarget:
    raw = dict(granule)
    meta = _mapping(raw.get("meta"), "meta")
    umm = _mapping(raw.get("umm"), "umm")
    collection_ref = _mapping(umm.get("CollectionReference"), "CollectionReference")
    collection = _required_str(collection_ref, "ShortName")
    collection_version = _required_str(collection_ref, "Version")
    if collection != expected_product:
        raise EMITProductError(
            f"CMR returned collection {collection!r}, expected {expected_product!r}"
        )
    if SUPPORTED_COLLECTIONS.get(collection) != collection_version:
        raise EMITProductError(
            f"CMR returned unreviewed {collection} version {collection_version!r}"
        )

    temporal = _mapping(umm.get("TemporalExtent"), "TemporalExtent")
    range_dt = _mapping(temporal.get("RangeDateTime"), "RangeDateTime")
    start_time = _parse_datetime(_required_str(range_dt, "BeginningDateTime"))
    end_time = _parse_datetime(_required_str(range_dt, "EndingDateTime"))
    attrs = _additional_attributes(umm.get("AdditionalAttributes"))
    concept_id = _required_str(meta, "concept-id")
    native_id = _required_str(meta, "native-id")
    provider_id = _required_str(meta, "provider-id")
    if provider_id != "LPCLOUD":
        raise EMITProductError(f"CMR returned provider {provider_id!r}, expected 'LPCLOUD'")

    platforms = umm.get("Platforms")
    if not isinstance(platforms, Sequence) or isinstance(platforms, (str, bytes)) or not platforms:
        raise EMITProductError("CMR EMIT record has no Platforms entry")
    platform = _mapping(platforms[0], "Platforms[0]")
    platform_id = _required_str(platform, "ShortName")
    instruments = platform.get("Instruments")
    if not isinstance(instruments, Sequence) or isinstance(instruments, (str, bytes)) or not instruments:
        raise EMITProductError("CMR EMIT record has no Instruments entry")
    sensor_id = _required_str(_mapping(instruments[0], "Instruments[0]"), "ShortName")

    assets, sizes, checksums = _extract_assets(umm)
    if not assets:
        raise EMITProductError(f"CMR target {native_id!r} exposes no protected HTTPS data assets")
    footprint = _footprint_wkt(umm)
    size_value = raw.get("size")
    try:
        size_mb = float(size_value) if size_value is not None else None
    except (TypeError, ValueError) as exc:
        raise EMITProductError(f"CMR target {native_id!r} has invalid size={size_value!r}") from exc

    return EMITTarget(
        product_id=native_id,
        title=_required_str(umm, "GranuleUR"),
        collection=collection,
        version=collection_version,
        concept_id=concept_id,
        collection_concept_id=_required_str(meta, "collection-concept-id"),
        provider_id=provider_id,
        platform_id=platform_id,
        sensor_id=sensor_id,
        start_time=start_time,
        end_time=end_time,
        footprint_wkt=footprint,
        cloud_cover=_optional_float(umm.get("CloudCover"), "CloudCover"),
        orbit=attrs.get("ORBIT"),
        orbit_segment=attrs.get("ORBIT_SEGMENT"),
        scene=attrs.get("SCENE"),
        solar_zenith_deg=_optional_float(attrs.get("SOLAR_ZENITH"), "SOLAR_ZENITH"),
        solar_azimuth_deg=_optional_float(attrs.get("SOLAR_AZIMUTH"), "SOLAR_AZIMUTH"),
        size_mb=size_mb,
        assets=assets,
        asset_sizes_bytes=sizes,
        checksums=checksums,
        catalogue_url=(
            "https://cmr.earthdata.nasa.gov/search/granules.umm_json?concept_id=" + concept_id
        ),
        retrieved_at=datetime.now(timezone.utc),
        raw=raw,
    )


def _extract_assets(
    umm: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, int], dict[str, tuple[str, str]]]:
    data_granule = _mapping(umm.get("DataGranule"), "DataGranule")
    records = data_granule.get("ArchiveAndDistributionInformation", [])
    sizes: dict[str, int] = {}
    checksums: dict[str, tuple[str, str]] = {}
    if isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
        for index, record_value in enumerate(records):
            record = _mapping(record_value, f"ArchiveAndDistributionInformation[{index}]")
            name = _required_str(record, "Name")
            if name.lower().endswith(".nc"):
                size = record.get("SizeInBytes")
                if not isinstance(size, int) or size <= 0:
                    raise EMITProductError(f"CMR asset {name!r} has invalid SizeInBytes={size!r}")
                sizes[name] = size
                checksum = _mapping(record.get("Checksum"), f"Checksum for {name}")
                checksums[name] = (
                    _required_str(checksum, "Algorithm").upper(),
                    _required_str(checksum, "Value").lower(),
                )

    assets: dict[str, str] = {}
    related = umm.get("RelatedUrls", [])
    if isinstance(related, Sequence) and not isinstance(related, (str, bytes)):
        for item_value in related:
            item = _mapping(item_value, "RelatedUrls entry")
            url = item.get("URL")
            if item.get("Type") != "GET DATA" or not isinstance(url, str):
                continue
            parsed = urlparse(url)
            filename = Path(parsed.path).name
            if (
                parsed.scheme == "https"
                and parsed.hostname == _DATA_HOST
                and filename.lower().endswith(".nc")
            ):
                assets[filename] = url
    if set(assets) != set(sizes):
        raise EMITProductError(
            "CMR HTTPS NetCDF assets disagree with archive/checksum metadata: "
            f"urls={sorted(assets)}, archive={sorted(sizes)}"
        )
    return assets, sizes, checksums


def _select_asset(target: EMITTarget, selector: str) -> tuple[str, str]:
    if selector in target.assets:
        return selector, target.assets[selector]
    upper_names = {name: name.upper() for name in target.assets}
    if selector == "uncertainty":
        matches = [name for name, upper in upper_names.items() if "UNCERT" in upper]
    elif selector == "mask":
        matches = [name for name, upper in upper_names.items() if "_MASK_" in upper]
    elif selector == "observation":
        matches = [name for name, upper in upper_names.items() if "_OBS_" in upper]
    elif selector == "primary":
        matches = [
            name for name, upper in upper_names.items()
            if "UNCERT" not in upper and "_MASK_" not in upper and "_OBS_" not in upper
        ]
    else:
        matches = []
    if len(matches) != 1:
        raise EMITDownloadError(
            f"asset selector {selector!r} matched {len(matches)} assets; "
            f"available filenames: {sorted(target.assets)}"
        )
    name = matches[0]
    return name, target.assets[name]


def _verify_checksum(path: Path, target: EMITTarget) -> None:
    checksum = target.checksums.get(path.name)
    if checksum is None:
        raise EMITDownloadError(f"CMR supplied no checksum for downloaded asset {path.name!r}")
    algorithm, expected = checksum
    try:
        digest = hashlib.new(algorithm.replace("-", "").lower())
    except ValueError as exc:
        raise EMITDownloadError(f"unsupported CMR checksum algorithm {algorithm!r}") from exc
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest().lower()
    if actual != expected.lower():
        raise EMITDownloadError(
            f"checksum mismatch for {path.name}: expected {algorithm} {expected}, got {actual}"
        )


def _additional_attributes(value: object) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return result
    for item_value in value:
        item = _mapping(item_value, "AdditionalAttributes entry")
        name = item.get("Name")
        values = item.get("Values")
        if (
            isinstance(name, str)
            and isinstance(values, Sequence)
            and not isinstance(values, (str, bytes))
            and values
        ):
            result[name] = str(values[0])
    return result


def _footprint_wkt(umm: Mapping[str, Any]) -> str:
    spatial = _mapping(umm.get("SpatialExtent"), "SpatialExtent")
    horizontal = _mapping(spatial.get("HorizontalSpatialDomain"), "HorizontalSpatialDomain")
    geometry = _mapping(horizontal.get("Geometry"), "Geometry")
    polygons = geometry.get("GPolygons")
    if not isinstance(polygons, Sequence) or isinstance(polygons, (str, bytes)) or len(polygons) != 1:
        raise EMITProductError("CMR EMIT record must contain exactly one GPolygon")
    boundary = _mapping(_mapping(polygons[0], "GPolygon").get("Boundary"), "Boundary")
    points = boundary.get("Points")
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)) or len(points) < 4:
        raise EMITProductError("CMR EMIT GPolygon has fewer than four points")
    coordinates: list[tuple[float, float]] = []
    for point_value in points:
        point = _mapping(point_value, "GPolygon point")
        coordinates.append((
            _required_float(point, "Longitude"),
            _required_float(point, "Latitude"),
        ))
    if coordinates[0] != coordinates[-1]:
        coordinates.append(coordinates[0])
    return "POLYGON ((" + ", ".join(f"{lon} {lat}" for lon, lat in coordinates) + "))"


def _footprint_centroid(wkt: str) -> tuple[float | None, float | None]:
    try:
        body = wkt.removeprefix("POLYGON ((").removesuffix("))")
        coordinates = [tuple(map(float, pair.split())) for pair in body.split(", ")]
    except (TypeError, ValueError):
        return None, None
    unique = coordinates[:-1] if len(coordinates) > 1 and coordinates[0] == coordinates[-1] else coordinates
    if not unique:
        return None, None
    return (
        sum(lat for _, lat in unique) / len(unique),
        sum(lon for lon, _ in unique) / len(unique),
    )


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EMITProductError(f"CMR field {field_name!r} is not an object")
    return value


def _required_str(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EMITProductError(f"CMR field {key!r} is missing or empty")
    return value


def _required_float(record: Mapping[str, Any], key: str) -> float:
    result = _optional_float(record.get(key), key)
    if result is None:
        raise EMITProductError(f"CMR field {key!r} is missing")
    return result


def _optional_float(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise EMITProductError(f"CMR field {field_name!r} is not numeric: {value!r}") from exc


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EMITProductError(f"invalid CMR datetime {value!r}") from exc
    if parsed.tzinfo is None:
        raise EMITProductError(f"CMR datetime {value!r} is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _as_start(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.combine(value, time.min)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_end(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.combine(value, time.max)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    west, south, east, north = bbox
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError(f"invalid EPSG:4326 bbox {bbox!r}")


def _local_path(raw: bytes | str) -> str | None:
    return None if isinstance(raw, bytes) else str(raw)
