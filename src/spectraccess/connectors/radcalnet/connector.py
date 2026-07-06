"""RadCalNet connector: RadCalNet's official JSON API (v2, live-verified).

RadCalNet publishes an official JSON API (documented by their own reference
client `radcalnet_api_client.py`, Magellium 2022, tech note ACTION-TN-074-MAG).
This connector implements the API shape independently -- it does NOT copy
Magellium's client code (which is "All rights reserved"); the API endpoints,
auth scheme, and response shapes below are verified live facts, not their
implementation.

Verified live 2026-07-07 (see spec p3_radcalnet):
- Base ``https://www.radcalnet.org/api/json/``; HTTP Basic auth on every
  request (``session.auth = (username, password)``); no signin dance, no
  cookies.
- ``GET api/json/`` -> JSON list of ``{"name": ...}`` site entries. The site
  list is dynamic (verified: 9 sites, more than the web UI shows) -- never
  hardcode it.
- ``GET api/json/{SITE}/data/`` -> JSON list of ``{"name": ...}`` ASCII daily
  files, named ``{SITEID}_{YYYY}_{DOY}_v{VV.VV}.input|.output``.
- ``GET api/json/{SITE}/data/{filename}`` -> the raw ASCII file.
- ``GET api/json/{SITE}/datanc/`` -> NetCDF files (out of v1 parse scope;
  ``discover(fmt="nc")`` lists them, ``parse()`` only handles ASCII ``.output``).
- Wrong/absent credentials -> HTTP 401.

``.output`` ASCII format per the official R2-DataFormatSpecification (V10),
the format authority on the RadCalNet portal. Value semantics (fill values,
negative-value = climatological flag) are drawn from that spec -- see
`_parse_output_text` and `_split_metadata_row` docstrings for the exact rules.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterable

import pandas as pd

from spectraccess.core.connector import Connector
from spectraccess.core.schema import Uncertainty, UncertaintyStatus, empty_frame, uncertainty_columns, validate
from spectraccess.core.session import CredentialConfig, CredentialSession

DEFAULT_BASE_URL = "https://www.radcalnet.org/api/json/"

# Fill-value family (R2-DataFormatSpecification V10): any value >= 9995 is a
# fill and carries no measurement, regardless of which of the five specific
# codes (9999 no data, 9998 not processed to TOA reflectance, 9997 anomalous
# atmosphere, 9996 anomalous surface, 9995 cloudy) is used.
_FILL_THRESHOLD = 9995.0

_OUTPUT_FILENAME_RE = re.compile(
    r"^(?P<site_id>[A-Z0-9]+)_(?P<year>\d{4})_(?P<doy>\d{3})_v(?P<version>\d{2}[.]\d{2})"
    r"[.](?P<kind>input|output)$"
)

# Per-time ancillary metadata rows -> canonical column names.
_ANCILLARY_ROWS: tuple[tuple[str, str], ...] = (
    ("Zen", "sza"),
    ("Azi", "saa"),
    ("AOD", "aod"),
    ("Ang", "angstrom"),
    ("WV", "water_vapour_g_cm2"),
    ("O3", "ozone_du"),
    ("P", "pressure_hpa"),
    ("T", "temperature_k"),
)


@dataclass(frozen=True)
class RadCalNetCredentials:
    username_env: str = "RADCALNET_USERNAME"
    password_env: str = "RADCALNET_PASSWORD"


@dataclass(frozen=True)
class RadCalNetTarget:
    """One file listed under ``api/json/{site}/data/`` (or ``datanc/``)."""

    site: str
    filename: str
    url: str
    fmt: str  # "ascii" | "nc"
    year: int | None = None
    doy: int | None = None
    version: str | None = None
    kind: str | None = None  # "input" | "output" | "archive" | None


def _credential_error(exc: Exception | None = None) -> ValueError:
    return ValueError(
        "RadCalNet credentials are required: set RADCALNET_USERNAME and "
        "RADCALNET_PASSWORD (a free RadCalNet portal account; spectrAccess "
        "ships no credentials of its own)."
    ) if exc is None else exc


class RadCalNetConnector(Connector):
    """Connector for RadCalNet's official JSON API."""

    def __init__(
        self,
        credentials: RadCalNetCredentials | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60,
    ) -> None:
        creds = credentials or RadCalNetCredentials()
        config = CredentialConfig(username_env=creds.username_env, password_env=creds.password_env)
        try:
            self.credential_session = CredentialSession(config=config)
        except ValueError as exc:
            raise _credential_error() from exc
        if self.credential_session.session.auth is None:
            raise _credential_error()
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    @property
    def _session(self):
        return self.credential_session.session

    def _get_json(self, url: str) -> list[dict]:
        response = self._session.get(url, timeout=self.timeout)
        if response.status_code == 401:
            raise _credential_error()
        response.raise_for_status()
        return response.json()

    def sites(self) -> list[str]:
        """Return the live list of RadCalNet site codes (dynamic -- never hardcoded)."""

        entries = self._get_json(self.base_url)
        return [entry["name"] for entry in entries]

    def discover(
        self,
        site: str | None = None,
        *,
        fmt: str = "ascii",
        kind: str | None = "output",
        start: tuple[int, int] | datetime | None = None,
        end: tuple[int, int] | datetime | None = None,
        **_kwargs: object,
    ) -> list[RadCalNetTarget]:
        """List files for one site (or every site when ``site`` is None).

        ``kind`` filters by parsed file kind (``"input"``/``"output"``/
        ``"archive"``); pass ``kind=None`` to disable the filter and include
        files that don't match the daily-filename pattern (e.g.
        ``GSCN_archive.nc``). ``start``/``end`` filter by (year, doy) window,
        inclusive on both ends -- accepts a ``(year, doy)`` tuple or a
        ``datetime`` (converted via its UTC year/day-of-year).
        """

        sites = [site] if site else self.sites()
        start_yd = _to_year_doy(start)
        end_yd = _to_year_doy(end)

        targets: list[RadCalNetTarget] = []
        for one_site in sites:
            subpath = "datanc" if fmt == "nc" else "data"
            listing_url = f"{self.base_url}{one_site}/{subpath}/"
            entries = self._get_json(listing_url)
            for entry in entries:
                filename = entry["name"]
                parsed = _parse_filename(filename)
                target_kind = parsed[3] if parsed else "archive"
                if parsed:
                    year, doy, version, target_kind = parsed
                else:
                    year = doy = version = None

                if kind is not None and target_kind != kind:
                    continue
                if start_yd is not None and year is not None and (year, doy) < start_yd:
                    continue
                if end_yd is not None and year is not None and (year, doy) > end_yd:
                    continue

                targets.append(
                    RadCalNetTarget(
                        site=one_site,
                        filename=filename,
                        url=f"{listing_url}{filename}",
                        fmt=fmt,
                        year=year,
                        doy=doy,
                        version=version,
                        kind=target_kind,
                    )
                )

        targets.sort(key=lambda t: (t.site, t.year or 0, t.doy or 0))
        return targets

    def fetch(self, target: RadCalNetTarget, *, dest: str | Path | None = None, **_kwargs: object) -> bytes | str:
        url = target.url if isinstance(target, RadCalNetTarget) else str(target)
        response = self._session.get(url, timeout=self.timeout, stream=dest is not None)
        if response.status_code == 401:
            raise _credential_error()
        response.raise_for_status()

        if dest is None:
            return response.content

        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=65536):
                fh.write(chunk)
        return str(dest_path)

    def parse(self, raw: bytes | str) -> pd.DataFrame:
        """Parse one ``.output`` file, or a ZIP of daily files, into a tidy frame.

        One row per (time, wavelength); see module docstring for the value
        semantics (fill/negative handling) applied to every numeric cell.
        """

        payload = _read_payload(raw)
        if _looks_like_zip(payload):
            return _parse_zip(payload)
        text = payload.decode("utf-8", errors="replace")
        return _parse_output_text(text, source_file=_guess_source_file(raw))

    def parse_canonical(
        self,
        raw: bytes | str,
        *,
        source_url: str | None = None,
        retrieved_at=None,
    ) -> pd.DataFrame:
        native = self.parse(raw)
        return to_canonical(native, source_url=source_url, retrieved_at=retrieved_at)


def to_canonical(
    native: pd.DataFrame,
    *,
    source_url: str | None = None,
    retrieved_at=None,
) -> pd.DataFrame:
    """Melt a native RadCalNet tidy frame into the spectrAccess canonical schema.

    One canonical row per native (time, wavelength) row. A non-empty native
    frame missing ``toa_reflectance`` raises ``ValueError`` (mirrors the
    GSICS connector's silent-drop guard: never turn a real parse failure into
    a quietly empty canonical frame).
    """

    if native.empty:
        return empty_frame()

    if "toa_reflectance" not in native.columns:
        raise ValueError(
            "native RadCalNet frame has no 'toa_reflectance' column -- cannot "
            f"build canonical rows (got columns: {list(native.columns)})"
        )

    rows: list[dict[str, object]] = []
    for _, native_row in native.iterrows():
        unc_status = native_row.get("toa_reflectance_unc_status", UncertaintyStatus.UNKNOWN.value)
        unc_value = native_row.get("toa_reflectance_unc")
        unc_value = float(unc_value) if pd.notna(unc_value) else None

        if unc_status == UncertaintyStatus.PROVIDED.value:
            unc = Uncertainty(value=unc_value, status=UncertaintyStatus.PROVIDED, k=None, provider="source")
        elif unc_status == UncertaintyStatus.PRIOR.value:
            unc = Uncertainty(value=unc_value, status=UncertaintyStatus.PRIOR, k=None, provider="source-climatology")
        else:
            unc = Uncertainty(value=None, status=UncertaintyStatus.UNKNOWN)

        row: dict[str, object] = {
            "time": native_row.get("timestamp"),
            "platform": None,
            "instrument": None,
            "band": None,
            "wavelength_nm": float(native_row["wavelength_nm"]),
            "site": native_row.get("site"),
            "latitude": native_row.get("lat"),
            "longitude": native_row.get("lon"),
            "reference": None,
            "quantity": "toa_reflectance",
            "value": float(native_row["toa_reflectance"]),
            "units": "1",
            "source": "radcalnet",
            "source_agency": "RadCalNet (CEOS WGCV)",
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "source_file": native_row.get("source_file"),
            "source_version": native_row.get("source_version"),
            "alt_m": native_row.get("alt_m"),
            "value_is_climatological": native_row.get("value_is_climatological"),
        }
        row.update(uncertainty_columns(unc))
        for _src_key, canonical_key in _ANCILLARY_ROWS:
            row[canonical_key] = native_row.get(canonical_key)
        rows.append(row)

    long_frame = pd.DataFrame(rows)
    return validate(long_frame)


def _to_year_doy(value: tuple[int, int] | datetime | None) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        as_utc = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        year_start = datetime(as_utc.year, 1, 1, tzinfo=timezone.utc)
        doy = (as_utc - year_start).days + 1
        return (as_utc.year, doy)
    return (int(value[0]), int(value[1]))


def _parse_filename(filename: str) -> tuple[int, int, str, str] | None:
    match = _OUTPUT_FILENAME_RE.match(filename)
    if not match:
        return None
    return (
        int(match.group("year")),
        int(match.group("doy")),
        match.group("version"),
        match.group("kind"),
    )


def _read_payload(raw: bytes | str) -> bytes:
    if isinstance(raw, bytes):
        return raw
    path = Path(raw)
    if path.exists():
        return path.read_bytes()
    return raw.encode("utf-8")


def _guess_source_file(raw: bytes | str | Path) -> str | None:
    if isinstance(raw, (str, Path)):
        path = Path(raw)
        if path.exists():
            return path.name
    return None


def _looks_like_zip(payload: bytes) -> bool:
    return payload[:2] == b"PK"


def _parse_zip(payload: bytes) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    with zipfile.ZipFile(BytesIO(payload)) as zf:
        names = zf.namelist()
        selected = _select_latest_output_entries(names)
        for name in selected:
            text = zf.read(name).decode("utf-8", errors="replace")
            source_file = Path(name).name
            frame = _parse_output_text(text, source_file=source_file)
            if not frame.empty:
                frames.append(frame)
    if not frames:
        return _empty_native_frame()
    return pd.concat(frames, ignore_index=True)


def _select_latest_output_entries(names: Iterable[str]) -> list[str]:
    """Pick the highest-version ``.output`` entry per (site, year, doy).

    Ported logic (filename regex + latest-version-per-day selection) from
    RefCal's `radcalnet_truth.py::_select_output_entries`; see module
    docstring -- logic ported, module never imported.
    """

    parsed: list[tuple[str, int, int, tuple[int, int], str]] = []
    for name in names:
        base = Path(name).name
        info = _parse_filename(base)
        if not info or info[3] != "output":
            continue
        year, doy, version, _kind = info
        version_tuple = tuple(int(p) for p in version.split("."))
        site_id = base.split("_", 1)[0]
        parsed.append((site_id, year, doy, version_tuple, name))

    latest: dict[tuple[str, int, int], tuple[tuple[int, int], str]] = {}
    for site_id, year, doy, version_tuple, name in parsed:
        key = (site_id, year, doy)
        if key not in latest or version_tuple > latest[key][0]:
            latest[key] = (version_tuple, name)
    return [value[1] for _key, value in sorted(latest.items())]


def _empty_native_frame() -> pd.DataFrame:
    columns = [
        "timestamp",
        "site",
        "lat",
        "lon",
        "alt_m",
        "wavelength_nm",
        "toa_reflectance",
        "value_is_climatological",
        "toa_reflectance_unc",
        "toa_reflectance_unc_status",
        "source_file",
        "source_version",
    ] + [canonical for _src, canonical in _ANCILLARY_ROWS]
    return pd.DataFrame(columns=columns)


def _utc_from_year_doy_time(year: int, doy: int, hhmm: str) -> datetime:
    hour_text, minute_text = hhmm.split(":", 1)
    base = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)
    return base.replace(hour=int(hour_text), minute=int(minute_text))


def _is_int_token(value: str) -> bool:
    try:
        int(value)
        return True
    except ValueError:
        return False


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_output_text(text: str, *, source_file: str | None) -> pd.DataFrame:
    """Parse one ``.output`` ASCII file's text into the native tidy frame.

    Sections (ported scan pattern from RefCal's `radcalnet_truth.py::
    parse_output_text` -- filename regex, metadata/spectral-block scan, and
    UTC construction are a direct logic port; the synthetic
    ``relative_sigma_k1`` fixed-uncertainty assumption is NOT ported -- this
    parser reads the real block-4 per-wavelength uncertainties instead):

    1. Site header: ``Site:``, ``Lat:``, ``Lon:``, ``Alt:`` (single value).
    2. Per-time metadata rows (``Year:``, ``DOY(U):``, ``UTC:``, ...).
    3. TOA reflectance spectral block: ``wavelength v1 ... vN``.
    4. A second metadata block (per-time atmospheric-parameter uncertainties)
       followed by the per-wavelength ABSOLUTE reflectance-uncertainty block.

    Value semantics (R2-DataFormatSpecification V10):
    - Fill family (any value >= 9995: 9999 no data, 9998 not processed to TOA
      reflectance, 9997 anomalous atmosphere, 9996 anomalous surface, 9995
      cloudy) carries no measurement.
    - A negative value flags "an average or climatological value" (spec
      section 1.x Notes) -- magnitude is valid, provenance is degraded.
      Reflectance: negative -> keep ``value=abs(v)``,
      ``value_is_climatological=True``. Fill reflectance -> skip the row
      entirely (no value, no row). Uncertainty: positive & non-fill ->
      ``unc_status="provided"``; negative -> ``unc_status="prior"``,
      ``unc_value=abs(u)``; fill or absent -> ``unc_status="unknown"``,
      ``unc_value=None``. ``unc_k`` is always None (R2 does not state a
      coverage factor; G4 is the uncertainty-methodology authority).
      Ancillary columns (P/T/WV/O3/AOD/Ang/Zen/Azi) apply the same fill->NaN,
      negative->abs(v) rule with no per-ancillary climatology flag in v1.
    """

    lines = text.splitlines()

    site_id: str | None = None
    lat: float | None = None
    lon: float | None = None
    alt_m: float | None = None

    metadata: dict[str, list[str]] = {}
    reflectance_rows: list[list[str]] = []
    uncertainty_metadata: dict[str, list[str]] = {}
    uncertainty_rows: list[list[str]] = []

    section = "header"
    blank_streak = 0
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            blank_streak += 1
            if section == "reflectance_block":
                section = "post_reflectance"
            continue
        blank_streak = 0
        parts = re.split(r"\s+", line)
        label = parts[0]

        if label == "Site:":
            site_id = parts[1] if len(parts) > 1 else None
            continue
        if label == "Lat:":
            lat = _to_float(parts[1]) if len(parts) > 1 else None
            continue
        if label == "Lon:":
            lon = _to_float(parts[1]) if len(parts) > 1 else None
            continue
        if label == "Alt:":
            alt_m = _to_float(parts[1]) if len(parts) > 1 else None
            continue

        if _is_int_token(label):
            if section in ("metadata", "header"):
                section = "reflectance_block"
                reflectance_rows.append(parts)
            elif section == "reflectance_block":
                reflectance_rows.append(parts)
            elif section in ("post_reflectance", "uncertainty_metadata"):
                section = "uncertainty_block"
                uncertainty_rows.append(parts)
            elif section == "uncertainty_block":
                uncertainty_rows.append(parts)
            continue

        if label.endswith(":"):
            key = label[:-1]
            if section in ("header", "metadata"):
                section = "metadata"
                metadata[key] = parts[1:]
            elif section == "post_reflectance":
                section = "uncertainty_metadata"
                uncertainty_metadata[key] = parts[1:]
            elif section == "uncertainty_metadata":
                uncertainty_metadata[key] = parts[1:]

    if site_id is None:
        raise ValueError("RadCalNet .output file has no 'Site:' header row")

    years = [int(v) for v in metadata.get("Year", [])]
    doys = [int(v) for v in metadata.get("DOY(U)", [])]
    utc_times = metadata.get("UTC", [])
    n_times = min(len(years), len(doys), len(utc_times))
    if n_times == 0:
        raise ValueError(f"RadCalNet .output file missing Year/DOY(U)/UTC rows: {source_file}")

    filename_info = _parse_filename(source_file) if source_file else None
    year_from_name, doy_from_name, version, _kind = filename_info or (None, None, None, None)

    ancillary_raw: dict[str, list[float | None]] = {}
    for src_key, _canonical in _ANCILLARY_ROWS:
        values = metadata.get(src_key, [])
        ancillary_raw[src_key] = [_to_float(v) for v in values[:n_times]]

    timestamps = [_utc_from_year_doy_time(years[i], doys[i], utc_times[i]) for i in range(n_times)]

    # Reflectance block: wavelength -> per-time raw string value.
    reflectance_by_wavelength: dict[int, list[str]] = {}
    wavelength_order: list[int] = []
    for row in reflectance_rows:
        wavelength = int(row[0])
        values = row[1 : 1 + n_times]
        if len(values) < n_times:
            continue
        reflectance_by_wavelength[wavelength] = values
        wavelength_order.append(wavelength)

    uncertainty_by_wavelength: dict[int, list[str]] = {}
    for row in uncertainty_rows:
        wavelength = int(row[0])
        values = row[1 : 1 + n_times]
        if len(values) < n_times:
            continue
        uncertainty_by_wavelength[wavelength] = values

    rows: list[dict[str, object]] = []
    for time_idx in range(n_times):
        ancillary_values = {
            canonical: _apply_fill_and_sign(ancillary_raw[src_key][time_idx])
            for src_key, canonical in _ANCILLARY_ROWS
        }
        for wavelength in wavelength_order:
            raw_value = _to_float(reflectance_by_wavelength[wavelength][time_idx])
            if raw_value is None or raw_value >= _FILL_THRESHOLD:
                continue  # fill reflectance -> skip the row entirely (no value, no row)
            is_climatological = raw_value < 0
            value = abs(raw_value)
            if not (0.0 <= value <= 1.0):
                continue

            unc_raw = None
            if wavelength in uncertainty_by_wavelength:
                unc_raw = _to_float(uncertainty_by_wavelength[wavelength][time_idx])

            if unc_raw is None or unc_raw >= _FILL_THRESHOLD:
                unc_status = UncertaintyStatus.UNKNOWN.value
                unc_value = None
            elif unc_raw < 0:
                unc_status = UncertaintyStatus.PRIOR.value
                unc_value = abs(unc_raw)
            else:
                unc_status = UncertaintyStatus.PROVIDED.value
                unc_value = unc_raw

            row: dict[str, object] = {
                "timestamp": timestamps[time_idx],
                "site": site_id,
                "lat": lat,
                "lon": lon,
                "alt_m": alt_m,
                "wavelength_nm": float(wavelength),
                "toa_reflectance": value,
                "value_is_climatological": is_climatological,
                "toa_reflectance_unc": unc_value,
                "toa_reflectance_unc_status": unc_status,
                "source_file": source_file,
                "source_version": version,
            }
            row.update(ancillary_values)
            rows.append(row)

    if not rows:
        return _empty_native_frame()

    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame


def _apply_fill_and_sign(value: float | None) -> float | None:
    if value is None or value >= _FILL_THRESHOLD:
        return None
    return abs(value)
