"""Fetch the pinned ESA document for local verification, never distribution."""

import hashlib
from pathlib import Path
from urllib.request import urlopen

from spectraccess.core.imagery.spectral import ESA_SRF_SHA256, ESA_SRF_URL

path = Path(__file__).resolve().parent / ".cache" / "s2_srf_v4.xlsx"
with urlopen(ESA_SRF_URL, timeout=60) as response:
    content = response.read()
if hashlib.sha256(content).hexdigest() != ESA_SRF_SHA256:
    raise ValueError("ESA download checksum differs from the v4.0 pin")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_bytes(content)
print(f"Verified ESA v4.0 SHA256 {ESA_SRF_SHA256}: {path}")
