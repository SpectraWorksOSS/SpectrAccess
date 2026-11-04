"""Regenerate frozen oracle JSON using a C compiler, no image rasters.

Usage: python tests/fixtures/imagery/build_landsat_oracle.py --cc gcc
       python ... --cc /absolute/path/zig.exe --zig
Builds executable in a temporary directory, source remains unmodified.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--cc", default="cc")
parser.add_argument("--zig", action="store_true")
parser.add_argument("--temp-root", default=None)
parser.add_argument("--ang", type=Path, default=HERE / "landsat_ANG.txt")
parser.add_argument("--output", type=Path, default=HERE / "landsat_oracle.json")
args = parser.parse_args()
vendor = HERE / "vendor" / "l8_angles"
names = [
    "ias_angle_gen_initialize.c", "ias_angle_gen_read_ang.c", "ias_angle_gen_find_scas.c",
    "ias_angle_gen_utilities.c", "ias_math_compute_unit_vector.c", "ias_math_compute_vector_length.c",
    "ias_odl_read_tree.c", "ias_odl_free_tree.c", "ias_odl_get_field.c", "lablib3.c",
    "ias_satellite_attributes.c", "landsat8.c", "ias_misc_convert_to_uppercase.c", "ias_logging.c",
]
with tempfile.TemporaryDirectory(prefix="oracle-", dir=args.temp_root) as directory:
    exe = Path(directory) / "oracle.exe"
    command = [args.cc] + (["cc"] if args.zig else []) + [
        "-O2", "-I", str(vendor), str(HERE / "landsat_oracle.c"),
        *[str(vendor / name) for name in names], "-o", str(exe), "-lm",
    ]
    print("Compile:", subprocess.list2cmdline(command), flush=True)
    env = dict(os.environ, TMP=directory, TEMP=directory, TMPDIR=directory)
    subprocess.run(command, check=True, env=env)
    result = subprocess.run([str(exe), str(args.ang)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"oracle exit {result.returncode}: {result.stderr}\n{result.stdout}")
    rows = json.loads(result.stdout)
    args.output.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print("Independent USGS per-(band,SCA) oracle:", len(rows), "samples", flush=True)
