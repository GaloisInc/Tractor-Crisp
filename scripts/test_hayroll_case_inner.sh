#!/usr/bin/env bash

set -euo pipefail

case_path="${HAYROLL_CASE_PATH:?missing HAYROLL_CASE_PATH}"
preset="${HAYROLL_CMAKE_PRESET:-test}"
project_dir="${HAYROLL_PROJECT_DIR:-}"
binary_name="${HAYROLL_BINARY_NAME:-}"

repo_root="/root/work"
case_dir="${repo_root}/${case_path}"

if [[ ! -d "$case_dir" ]]; then
    echo "error: case directory does not exist in container: ${case_dir}" >&2
    exit 1
fi

if [[ -z "$project_dir" ]]; then
    if [[ -d "${case_dir}/test_case" ]]; then
        project_dir="test_case"
    else
        project_dir="."
    fi
fi

build_dir="build-ninja"
cmake_args=()

if [[ -f "${case_dir}/CMakePresets.json" ]]; then
    build_dir="$(
python3 - "$case_dir/CMakePresets.json" "$preset" <<'PY'
import json
import sys
from pathlib import Path

presets_path = Path(sys.argv[1])
preset_name = sys.argv[2]
data = json.loads(presets_path.read_text())
presets = {p["name"]: p for p in data.get("configurePresets", [])}

def merged(name: str) -> dict:
    if name not in presets:
        raise SystemExit(f"unknown CMake configure preset: {name}")
    preset = dict(presets[name])
    inherits = preset.get("inherits", [])
    if isinstance(inherits, str):
        inherits = [inherits]
    merged_preset = {}
    for parent in inherits:
        merged_preset.update(merged(parent))
    merged_preset.update(preset)
    return merged_preset

resolved = merged(preset_name)
binary_dir = resolved.get("binaryDir")
if not binary_dir:
    raise SystemExit(f"preset {preset_name!r} has no binaryDir")
print(binary_dir)
PY
)"
    cmake_args=(--preset "$preset" -DCMAKE_EXPORT_COMPILE_COMMANDS=ON)
elif [[ -f "${case_dir}/CMakeLists.txt" ]]; then
    cmake_args=(
        -S .
        -B "$build_dir"
        -G Ninja
        -DCMAKE_BUILD_TYPE=Release
        -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
    )
elif [[ -f "${case_dir}/test_case/CMakeLists.txt" ]]; then
    cmake_args=(
        -S test_case
        -B "$build_dir"
        -G Ninja
        -DCMAKE_BUILD_TYPE=Release
        -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
    )
else
    echo "error: no supported CMake entrypoint found under ${case_dir}" >&2
    exit 1
fi

test_corpus_root="/root/work/Test-Corpus"
case_rel="$case_path"
if [[ "$case_rel" == Test-Corpus/* ]]; then
    case_rel="${case_rel#Test-Corpus/}"
fi

export PYTHONPATH="${test_corpus_root}/deployment/scripts/github-actions"

cd "$case_dir"

echo "==> Configuring ${case_path}"
printf "==> Running"
printf " %q" cmake "${cmake_args[@]}"
printf "\n"
cmake "${cmake_args[@]}"

echo "==> Normalizing compile_commands.json for hayroll"
"/root/work/scripts/fix_compile_commands.py" "${build_dir}/compile_commands.json"

hayroll_args=(
    "${build_dir}/compile_commands.json"
    "translated_rust"
    "--project-dir" "$project_dir"
    "--keep-src-loc"
)

if [[ -n "$binary_name" ]]; then
    hayroll_args+=("--binary" "$binary_name")
fi

printf "==> Running"
printf " %q" hayroll "${hayroll_args[@]}"
printf "\n"
hayroll "${hayroll_args[@]}"

if [[ -d "${case_dir}/runner" && -z "$binary_name" ]]; then
    echo "==> Patching generated Cargo.toml for runner-loaded shared library"
    lib_name="$(sed -n 's/^[[:space:]]*library:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "${case_dir}/runner/src/main.rs" | sed -n '1p')"
    if [[ -z "$lib_name" ]]; then
        lib_name="$(basename "$case_dir")"
    fi
    "/root/work/scripts/patch_hayroll_cargo.py" \
        "$case_dir" \
        "${case_dir}/translated_rust/Cargo.toml" \
        "$lib_name"
fi

echo "==> Running C tests"
python3 -m runtests.ci --root "$test_corpus_root" -s "$case_rel"

echo "==> Running Rust tests"
python3 -m runtests.rust --root "$test_corpus_root" -s "$case_rel" --verbose
