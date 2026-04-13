#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///

import json
import shlex
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print(
            f"usage: {Path(sys.argv[0]).name} <compile_commands.json>",
            file=sys.stderr,
        )
        return 2

    path = Path(sys.argv[1])
    data = json.loads(path.read_text())
    for entry in data:
        if "arguments" not in entry and "command" in entry:
            entry["arguments"] = shlex.split(entry["command"])
    path.write_text(json.dumps(data, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
