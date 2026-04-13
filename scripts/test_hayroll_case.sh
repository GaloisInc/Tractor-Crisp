#!/usr/bin/env bash

set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/test_hayroll_case.sh [options] <case-path>

Run the full hayroll-on-Test-Corpus loop for one case inside the
`tractor-crisp-user` Docker image:
1. configure the case with CMake
2. generate compile_commands.json
3. run hayroll
4. run the existing C and Rust test runners

`<case-path>` should be a repo-relative path such as:
  Test-Corpus/Public-Tests/P01_sphincs_plus/005_sphincs_PQCgenKAT_sign_blake_128f_simple

Options:
  --image <name>        Docker image to run (default: tractor-crisp-user)
  --preset <name>       CMake configure preset (default: test)
  --project-dir <path>  Path inside the case to pass to hayroll --project-dir
                        (default: test_case if present, else .)
  --binary <name>       Pass hayroll --binary <name>
  -h, --help            Show this help
EOF
}

image="tractor-crisp-user"
preset="test"
project_dir=""
binary_name=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --image)
            image="$2"
            shift 2
            ;;
        --preset)
            preset="$2"
            shift 2
            ;;
        --project-dir)
            project_dir="$2"
            shift 2
            ;;
        --binary)
            binary_name="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "error: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            break
            ;;
    esac
done

if [[ $# -ne 1 ]]; then
    usage >&2
    exit 2
fi

case_path="$1"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
inner_script="${repo_root}/scripts/test_hayroll_case_inner.sh"

if [[ ! -f "$inner_script" ]]; then
    echo "error: inner script was not found: ${inner_script}" >&2
    exit 1
fi

case_host_dir="${repo_root}/${case_path}"
if [[ ! -d "$case_host_dir" ]]; then
    echo "error: case directory does not exist: ${case_host_dir}" >&2
    exit 1
fi

if [[ ! -f "${case_host_dir}/CMakePresets.json" ]] \
    && [[ ! -f "${case_host_dir}/CMakeLists.txt" ]] \
    && [[ ! -f "${case_host_dir}/test_case/CMakeLists.txt" ]]; then
    echo "error: no supported CMake entrypoint found under ${case_host_dir}" >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker is not installed or not on PATH" >&2
    exit 1
fi

if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "error: Docker image '$image' was not found" >&2
    echo "build it first with:" >&2
    echo "  docker build --target tractor-crisp-user --tag tractor-crisp-user ." >&2
    exit 1
fi

tty_flags=()
if [[ -t 0 && -t 1 ]]; then
    tty_flags=(-it)
fi

docker run --rm "${tty_flags[@]}" \
    -v "${repo_root}:/root/work" \
    -w /root/work \
    -e HAYROLL_CASE_PATH="$case_path" \
    -e HAYROLL_CMAKE_PRESET="$preset" \
    -e HAYROLL_PROJECT_DIR="$project_dir" \
    -e HAYROLL_BINARY_NAME="$binary_name" \
    "$image" \
    /root/work/scripts/test_hayroll_case_inner.sh
