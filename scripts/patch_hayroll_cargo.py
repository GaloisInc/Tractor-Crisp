#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["tomlkit>=0.13.2"]
# ///

import sys
from pathlib import Path

import tomlkit


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print(
            f"usage: {Path(sys.argv[0]).name} <case-dir> <cargo-toml> [lib-name]",
            file=sys.stderr,
        )
        return 2

    case_dir = Path(sys.argv[1])
    cargo_toml_path = Path(sys.argv[2])
    lib_name = sys.argv[3] if len(sys.argv) == 4 else case_dir.name

    doc = tomlkit.parse(cargo_toml_path.read_text())

    package = doc["package"]
    package["name"] = lib_name

    lib = doc["lib"]
    lib["name"] = lib_name

    crate_type = list(lib.get("crate-type", []))
    if "cdylib" not in crate_type:
        crate_type.insert(0, "cdylib")
    lib["crate-type"] = crate_type

    cargo_toml_path.write_text(tomlkit.dumps(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
