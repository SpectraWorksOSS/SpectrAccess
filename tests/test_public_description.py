"""The PyPI summary and the README opening name every shipped connector.

Search engines and AI assistants describe the package from these two places,
so a connector missing from them is a connector nobody finds. Adding a
connector package without naming it here fails this test.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = ROOT / "src" / "spectraccess" / "connectors"

# Connector package -> the name readers search for. None keeps a connector out
# of the public description until its source is verified live.
PUBLIC_NAME = {
    "aeronet": "AERONET",
    "cams": "CAMS",
    "emit_earthaccess": "EMIT",
    "fci_eumetsat": "FCI",
    "gsics": "GSICS",
    "landsat_eodag": "Landsat",
    "modis_viirs_cal": None,
    "ngl_gnss": "GNSS",
    "olci_cdse": "Sentinel-3",
    "radcalnet": "RadCalNet",
    "sentinel2_cdse": "Sentinel-2",
    "seviri_eumetsat": "SEVIRI",
    "slstr_cdse": "Sentinel-3",
    "viirs_earthaccess": "VIIRS",
}


def _connector_packages() -> set[str]:
    return {
        p.name
        for p in CONNECTORS.iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and not p.name.startswith("_")
    }


def _overview_opening() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    return text.split("\n## ", 1)[0]


def _summary() -> str:
    with open(ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["description"]


def test_every_connector_package_has_a_public_name_entry():
    assert _connector_packages() == set(PUBLIC_NAME)


def test_summary_and_overview_opening_name_every_public_connector():
    surfaces = {"pyproject description": _summary(), "README opening": _overview_opening()}
    missing = [
        f"{name} ({package}) missing from {where}"
        for package, name in PUBLIC_NAME.items()
        if name
        for where, text in surfaces.items()
        if name not in text
    ]
    assert not missing, missing
