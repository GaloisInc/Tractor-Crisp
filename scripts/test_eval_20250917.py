#!/usr/bin/env -S uv run

from dataclasses import dataclass
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


@dataclass
class Args:
    project_dir: Path
    main_compilation_unit: str | None


def parse_args() -> Args:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir", type=Path)
    ap.add_argument(
        "--main-compilation-unit",
        help="name of the compilation unit that defines `main`",
    )
    return Args(**ap.parse_args().__dict__)


def get_target_info(project_dir: Path):
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        query_file = td / ".cmake/api/v1/query/codemodel-v2"
        query_file.parent.mkdir(parents=True)
        query_file.open("w")

        subprocess.run(
            ("cmake", project_dir / "test_case/CMakeLists.txt"),
            cwd=td,
            check=True,
        )

        reply_dir = td / ".cmake/api/v1/reply"

        index_jsons = [
            f
            for f in reply_dir.iterdir()
            if f.name.startswith("index-") and f.suffix == ".json"
        ]
        assert len(index_jsons) == 1, f"got multiple index.json files: {index_jsons!r}"
        index_json = index_jsons[0]

        j_index = json.loads(index_json.read_text())

        codemodel_json = j_index["reply"]["codemodel-v2"]["jsonFile"]
        assert isinstance(codemodel_json, str)

        j_cm = json.loads((reply_dir / codemodel_json).read_text())

        target_jsons = [
            j_target["jsonFile"]
            for j_cfg in j_cm["configurations"]
            for j_target in j_cfg["targets"]
        ]
        # Expect one build target per project for now.
        # assert len(target_jsons) == 1, f"got multiple build targets: {target_jsons!r}"
        target_json = target_jsons[0]

        j_target = json.loads((reply_dir / target_json).read_text())
        return j_target


def file_contains_main(path: Path) -> bool:
    p = subprocess.run(
        ("ctags", "-x", path), stdout=subprocess.PIPE, text=True, check=True
    )
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) > 0 and parts[0] == "main":
            return True
    return False


def find_file_containing_main(project_dir: Path, j_target) -> str | None:
    for j_source in j_target["sources"]:
        path = Path(j_source["path"])
        full_path = project_dir / "test_case" / path
        if file_contains_main(full_path):
            return path.stem
    return None


def find_git_root(path: Path) -> Path:
    orig_path = path
    while True:
        # Use `.is_dir()` instead of `.exists()` so that the `gitdir: ...` files
        # placed in submodule roots will be ignored.
        if (path / ".git").is_dir():
            return path
        new_path = path.parent
        assert new_path != path, f"found no .git directory above {orig_path!r}"
        path = new_path


def run_crisp(cli_args: Args, *args, **kwargs):
    if "cwd" not in kwargs:
        kwargs["cwd"] = cli_args.project_dir
    if "check" not in kwargs:
        kwargs["check"] = True

    crisp_dir = Path(__file__).parent.parent.absolute()
    cmd = ("uv", "run", "--project", crisp_dir, "crisp", *args)

    return subprocess.run(cmd, **kwargs)


LIB_CONFIG_STR = r'''
base_dir = "{base_dir}"
project_name = "{example_name}"
# Hack: some tests have nested directories; just add enough separate glob
# patterns to cover them all.
src_globs = [
    "translated_rust/src/*.rs",
    "translated_rust/src/*/*.rs",
    "translated_rust/src/*/*/*.rs",
    "translated_rust/src/*/*/*/*.rs",
]
test_command = """
set -e
export PYTHONPATH=$PWD/deployment/scripts/github-actions
cd {example_dir}
# Run non-Rust tests first so the C .so will be available for the Rust tests
python3 -m runtests.ci --root ../../.. -s {example_dir}
python3 -m runtests.rust --root ../../.. -s {example_dir} --verbose
"""

[transpile]
cmake_src_dir = "test_case"
output_dir = "translated_rust"
single_target = "{example_name}"
'''

BIN_CONFIG_STR = r'''
base_dir = "{base_dir}"
project_name = "{example_name}"
src_globs = [
    "translated_rust/src/*.rs",
    "translated_rust/src/*/*.rs",
    "translated_rust/src/*/*/*.rs",
    "translated_rust/src/*/*/*/*.rs",
]
test_command = """
set -e
export PYTHONPATH=$PWD/deployment/scripts/github-actions
cd {example_dir}
# Run non-Rust tests first so the C .so will be available for the Rust tests
python3 -m runtests.ci --root ../../.. -s {example_dir}
python3 -m runtests.rust --root ../../.. -s {example_dir} --verbose
"""

[transpile]
cmake_src_dir = "test_case"
output_dir = "translated_rust"
bin_main = "{main_compilation_unit}"
single_target = "{example_name}"
'''


def relpath(path: Path, start: Path) -> Path:
    # Use `os.path.relpath` instead of `Path.relative_to`
    # since these paths aren't inside `args.project_dir`.
    return Path(os.path.relpath(path, start))


def main():
    args = parse_args()

    # Extract CMake target info
    target_info = get_target_info(args.project_dir)

    # Write crisp.toml
    base_dir = find_git_root(args.project_dir)
    example_dir_rel = args.project_dir.relative_to(base_dir)

    match target_info["type"]:
        case "STATIC_LIBRARY" | "SHARED_LIBRARY":
            cfg_template = LIB_CONFIG_STR
            main_compilation_unit = None
        case "EXECUTABLE":
            cfg_template = BIN_CONFIG_STR
            main_compilation_unit = args.main_compilation_unit
            if main_compilation_unit is None:
                main_compilation_unit = find_file_containing_main(
                    args.project_dir, target_info
                )
                print(f"autodetected main compilation unit = {main_compilation_unit!r}")
            if main_compilation_unit is None:
                raise ValueError(
                    "--main-compilation-unit is unset and autodetection failed"
                )
        case ty:
            raise ValueError(f"unknown CMake target type {ty!r}")

    cfg_str = cfg_template.format(
        base_dir=str(relpath(base_dir, args.project_dir)),
        example_dir=str(example_dir_rel),
        example_name=target_info["name"],
        main_compilation_unit=main_compilation_unit,
    )
    (args.project_dir / "crisp.toml").write_text(cfg_str)

    # Collect source files
    commit_files = [
        base_dir / "Cargo.toml",
    ]
    commit_dirs = [
        args.project_dir / "runner",
        args.project_dir / "test_case",
        args.project_dir / "test_vectors",
        base_dir / "deployment",
        base_dir / "tools",
    ]

    src_files = [relpath(path, args.project_dir) for path in commit_files]
    for start_dir in commit_dirs:
        for root, dirs, files in start_dir.walk():
            for f in files:
                path = root / f
                rel_path = relpath(path, args.project_dir)
                src_files.append(rel_path)
            for i in reversed(range(len(dirs))):
                if dirs[i] in ("target", "__pycache__"):
                    del dirs[i]

    run_crisp(args, "commit", "-t", "c_code", *src_files)
    run_crisp(args, "main")


if __name__ == "__main__":
    main()
