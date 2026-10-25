"""Refresh public metadata fixtures ONLY. Never downloads image rasters.

Run from workspace root: python tests/fixtures/imagery/fetch_metadata.py
Source URLs and SHA256s are retained alongside the fixtures.
"""

import hashlib
import io
import json
from pathlib import Path
import tarfile
import xml.etree.ElementTree as ET

import requests

HERE = Path(__file__).resolve().parent
manifest = {}


def fetch(url, dest):
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    manifest[dest.relative_to(HERE).as_posix()] = {
        "url": url, "sha256": hashlib.sha256(response.content).hexdigest(),
    }
    return response.content


landsat = "LC08/01/190/030/LC08_L1GT_190030_20131125_20170428_01_T2/LC08_L1GT_190030_20131125_20170428_01_T2_ANG.txt"
fetch("https://storage.googleapis.com/gcp-public-data-landsat/" + landsat, HERE / "landsat_ANG.txt")

# Anonymous archive read: signed URL is transient and is never recorded.
catalog = requests.get("https://planetarycomputer.microsoft.com/api/stac/v1/search", params={
    "collections": "landsat-c2-l2", "bbox": "-114,35,-113,36",
    "datetime": "2024-05-01T00:00:00Z/2024-05-30T00:00:00Z", "limit": 1,
    "query": json.dumps({"platform": {"eq": "landsat-9"}}),
}, timeout=60)
catalog.raise_for_status()
item = catalog.json()["features"][0]
url = item["assets"]["ang"]["href"]
signature = requests.get("https://planetarycomputer.microsoft.com/api/sas/v1/sign", params={"href": url}, timeout=60)
signature.raise_for_status()
response = requests.get(signature.json()["href"], timeout=60)
response.raise_for_status()
(HERE / "landsat9_C2_ANG.txt").write_bytes(response.content)
manifest["landsat9_C2_ANG.txt"] = {"url": url, "sha256": hashlib.sha256(response.content).hexdigest(),
    "product_id": item["id"], "note": "Collection-2 ANG shipped with L2; contains original per-band L1T/L1R geometric model"}

for date in ("20240501", "20210507"):
    response = requests.get("https://storage.googleapis.com/storage/v1/b/gcp-public-data-sentinel-2/o",
                            params={"prefix": "tiles/31/U/FT/S2B_MSIL1C_" + date, "maxResults": 200}, timeout=60)
    response.raise_for_status()
    for item in response.json().get("items", []):
        name = item["name"]
        if name.endswith(("MTD_TL.xml", "MTD_DS.xml", "MTD_MSIL1C.xml")):
            dest = HERE / (date + "_" + Path(name).name)
            content = fetch("https://storage.googleapis.com/gcp-public-data-sentinel-2/" + name, dest)
            if name.endswith("MTD_DS.xml"):
                root = ET.fromstring(content)
                def prune(el):
                    if el.tag.rsplit("}", 1)[-1] in {"Time_Stamp", "GPS_Points_List"}:
                        return True
                    for child in list(el):
                        if not prune(child):
                            el.remove(child)
                    return bool(len(el))
                prune(root)
                content = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                dest.write_bytes(content)
                entry = manifest[dest.relative_to(HERE).as_posix()]
                entry["provider_sha256"] = entry["sha256"]
                entry["sha256"] = hashlib.sha256(content).hexdigest()
                entry["extraction"] = "Only Time_Stamp and GPS_Points_List, ancestor hierarchy retained; no values altered"

# Vendor only the unmodified polynomial evaluator and its independent C
# oracle source, not the distribution's sample imagery.
url = "https://raw.githubusercontent.com/brazil-data-cube/landsat-angles-docker/master/L8_ANGLES_2_7_0.tgz"
response = requests.get(url, timeout=60)
response.raise_for_status()
archive = tarfile.open(fileobj=io.BytesIO(response.content))
for member in archive.getmembers():
    if not member.name.startswith("./l8_angles/ias_lib/") or not member.name.endswith((".c", ".h")):
        continue
    name = Path(member.name).name
    content = archive.extractfile(member).read()
    dest = HERE / "vendor" / "l8_angles" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    manifest[dest.relative_to(HERE).as_posix()] = {"url": url, "member": name,
        "archive_sha256": hashlib.sha256(response.content).hexdigest(), "sha256": hashlib.sha256(content).hexdigest()}

# Pin the independent reader used by the S2 metadata parser oracle.
api = requests.get("https://api.github.com/repos/contrailcirrus/pycontrails/commits/main", timeout=60)
api.raise_for_status()
commit = api.json()["sha"]
fetch("https://raw.githubusercontent.com/contrailcirrus/pycontrails/" + commit + "/pycontrails/datalib/leo_utils/sentinel_metadata.py",
      HERE / "vendor" / "sentinel_metadata.py")
fetch("https://raw.githubusercontent.com/contrailcirrus/pycontrails/" + commit + "/LICENSE",
      HERE / "vendor" / "PYCONTRAILS_LICENSE")
notice_path = HERE / "vendor" / "PYCONTRAILS_NOTICE"
notice = fetch("https://raw.githubusercontent.com/contrailcirrus/pycontrails/" + commit + "/NOTICE", notice_path)
notice_path.write_bytes(notice.splitlines()[0] + b"\n")
entry = manifest["vendor/PYCONTRAILS_NOTICE"]
entry["provider_sha256"] = entry["sha256"]
entry["sha256"] = hashlib.sha256(notice_path.read_bytes()).hexdigest()
entry["extraction"] = "Copyright attribution retained; unrelated CoCiP attribution omitted"
# ESA SRF document is fetched separately into an ignored cache.
(HERE / "sources.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print("Metadata fixtures:", len(manifest), "files; no image rasters")
