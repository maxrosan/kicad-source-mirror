"""Generate the Python protobuf bindings for the KiCad IPC API.

The bindings are generated from the .proto files of this repository (api/proto), so they
always match the KiCad built from the same checkout. Run it again after pulling KiCad
updates that touch api/proto.
"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTO_ROOT = HERE.parent / "api" / "proto"
OUT_DIR = HERE / "kicad_mcp" / "_proto"


def main() -> int:
    protoc = shutil.which("protoc")

    if not protoc:
        print("protoc not found; activate the conda env 'kicad' first", file=sys.stderr)
        return 1

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)

    OUT_DIR.mkdir(parents=True)

    protos = sorted(p.relative_to(PROTO_ROOT).as_posix() for p in PROTO_ROOT.rglob("*.proto"))
    cmd = [protoc, "-I.", f"--python_out={OUT_DIR}", *protos]
    result = subprocess.run(cmd, cwd=PROTO_ROOT)

    if result.returncode != 0:
        return result.returncode

    # protoc emits flat packages (common/, board/, schematic/); make them importable.
    for directory in [OUT_DIR, *[d for d in OUT_DIR.rglob("*") if d.is_dir()]]:
        (directory / "__init__.py").touch()

    print(f"Generated {len(protos)} proto files into {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
