#!/usr/bin/env -S uv run

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
import argparse
import json
import os
import shlex
import subprocess
import tempfile
import toml


@dataclass
class Args:
    project_dir: Path
    extra_test_dirs: list[Path]
    runtests: bool
    git: bool
    main_args: list[str]


def parse_args() -> Args:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir", type=Path)
    ap.add_argument('--extra-test-dir',
        type=Path, action='append', default=[], dest='extra_test_dirs',
        help='run extra test vectors from this directory')
    ap.add_argument("--no-runtests", action='store_false', dest='runtests',
        help="don't import `runtests` scripts or use them in `test_command`")
    ap.add_argument("--no-git", action='store_false', dest='git',
        help="don't require the project dir to be inside a git repository "
            '(implies --no-runtests)')
    ap.add_argument("main_args", nargs='*',
        help='extra arguments to pass to `crisp main`')
    return Args(**ap.parse_args().__dict__)


def get_target_info(project_dir: Path, extra_args: list[str]):
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        query_file = td / ".cmake/api/v1/query/codemodel-v2"
        query_file.parent.mkdir(parents=True)
        query_file.open("w")

        print(["cmake", project_dir.absolute() / 'CMakeLists.txt'] +
                extra_args)
        subprocess.run(
            ["cmake", project_dir.absolute() / 'CMakeLists.txt', '-B', '.'] + extra_args,
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

        j_targets = [json.loads((reply_dir / target_json).read_text())
                for target_json in target_jsons]
        return j_targets


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
        full_path = project_dir / path
        if file_contains_main(full_path):
            return path.stem
    return None


def find_git_root(path: Path) -> Path:
    orig_path = path
    while True:
        # Use `.exists()` to count both `.git/` directories
        # and `.git` submodule files (containing `gitdir: ...`).
        if (path / ".git").exists():
            return path
        new_path = path.parent
        assert new_path != path, f"found no .git directory above {orig_path!r}"
        path = new_path


def run_crisp(cli_args: Args, args: Sequence[str | Path]):
    crisp_dir = Path(__file__).parent.parent.absolute()
    cmd = ["uv", "run", "--project", crisp_dir, "crisp", *args]
    #print(f'running {cmd!r}')
    return subprocess.run(cmd, cwd=cli_args.project_dir, check=True)


# Note: all paths in the config are relative to the example/project directory
# where the config file is located, except for paths in `test_command`, which
# is run from the base directory.
CONFIG_TEMPLATE_STR = r'''
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
test_command = """{test_command}"""

[transpile]
output_dir = "translated_rust"
'''

TEST_COMMAND_PREAMBLE = """
set -e
export PYTHONPATH=$PWD/deployment/scripts/github-actions
# Run non-Rust tests first so the C .so will be available for the Rust tests
python3 -m runtests.ci --root . -s {project_dir_from_base_quoted}
python3 -m runtests.rust --root . -s {project_dir_from_base_quoted} --verbose
"""


def relpath(path: Path, start: Path) -> Path:
    # Use `os.path.relpath` instead of `Path.relative_to`
    # since these paths aren't inside `args.project_dir`.
    return Path(os.path.relpath(path, start))


def main():
    args = parse_args()

    if not args.git:
        args.runtests = False
    if not args.runtests:
        assert len(args.extra_test_dirs) == 0, \
            '--extra-test-dirs is not supported with --no-runtests'

    # For consistency, `foo_dir` is always an absolute path in this code.
    # Relative paths are always `foo_dir_from_bar`, meaning the path of
    # `foo_dir` relative to `bar_dir`.
    args.project_dir = args.project_dir.resolve()
    args.extra_test_dirs = [path.resolve() for path in args.extra_test_dirs]

    # `cmake_dir` is the directory that contains the top-level
    # `CMakeLists.txt`.
    cmake_dir = args.project_dir
    cmakelists_path = cmake_dir / 'CMakeLists.txt'
    if not cmakelists_path.exists():
        cmake_dir = cmake_dir / 'test_case'
        new_cmakelists_path = cmake_dir / 'CMakeLists.txt'
        assert new_cmakelists_path.exists(), \
            f'CMakeLists.txt not found at {cmakelists_path} or {new_cmakelists_path}'

    cmake_extra_args = []

    cmake_presets_path = cmake_dir / 'CMakePresets.json'
    if cmake_presets_path.exists():
        j_presets = json.load(cmake_presets_path.open())
        assert len(j_presets['buildPresets']) == 1, 'expected exactly one entry in buildPresets'
        preset_name = j_presets['buildPresets'][0]['name']
        cmake_extra_args.extend(('--preset', preset_name))

    # Extract CMake target info
    targets = get_target_info(cmake_dir, cmake_extra_args)

    # Write crisp.toml
    if args.git:
        base_dir = find_git_root(args.project_dir)
    else:
        base_dir = args.project_dir
    project_dir_from_base = args.project_dir.relative_to(base_dir)
    base_dir_from_project = relpath(base_dir, args.project_dir)
    cmake_dir_from_base = cmake_dir.relative_to(base_dir)
    cmake_dir_from_project = cmake_dir.relative_to(args.project_dir)

    # Assemble test commands
    if args.runtests:
        test_command_parts = [
            TEST_COMMAND_PREAMBLE.format(
                project_dir_from_base_quoted = shlex.quote(str(project_dir_from_base)),
            ),
        ]
        for test_dir in args.extra_test_dirs:
            test_dir_from_base = test_dir.relative_to(base_dir)
            test_dir_from_base_quoted = shlex.quote(str(test_dir_from_base))
            project_dir_from_test = relpath(args.project_dir, test_dir)
            project_dir_from_test_quoted = shlex.quote(str(project_dir_from_test))
            test_command_parts.extend((
                f'ln -sf {project_dir_from_test_quoted}/test_case {test_dir_from_base_quoted}/test_case',
                f'ln -sf {project_dir_from_test_quoted}/translated_rust {test_dir_from_base_quoted}/translated_rust',
                f'python3 -m runtests.ci -s {test_dir_from_base_quoted} --verbose',
                f'python3 -m runtests.rust -s {test_dir_from_base_quoted} --verbose',
                # `commit_dir` currently doesn't support symlinks, so remove them
                # when done.
                f'rm {test_dir_from_base_quoted}/test_case',
                f'rm {test_dir_from_base_quoted}/translated_rust',
            ))
    else:
        test_command_parts = [
            f'cd {shlex.quote(str(project_dir_from_base / "translated_rust"))}',
            'cargo build',
        ]

    cfg_parts = [
        CONFIG_TEMPLATE_STR.format(
            base_dir = base_dir_from_project,
            example_name = targets[0]['name'],
            project_dir_from_base_quoted = shlex.quote(str(project_dir_from_base)),
            test_command = '\n'.join(test_command_parts),
        ),
    ]

    # TODO (hack): since several of CRISP's helper tools don't support multiple
    # Rust crates, we try to combine everything into one crate instead.
    #
    # If there's only one artifact (a.k.a. target), we simply transpile that
    # artifact.  This handles single-library and single-binary projects.
    #
    # If there's a binary and several libraries (as in P01), we proceed as
    # follows (this part is the hack).  We do a clean build of the binary under
    # `bear`, which picks up not only the `.c` files for the binary, but also
    # those for any library it depends on.  We run c2rust-transpile on the
    # `compile_commands.json` that was generated this way, which produces a
    # Rust version of the binary.  Then, for each library artifact, we
    # configure Cargo to build the existing source code of the *binary* as a
    # library of the appropriate name.  As long as the binary depends on the
    # library in question, the library's source code will have been included in
    # the binary, and the library compiled from that binary will export all of
    # the right symbols.  However, it will also include the symbols from all
    # the other libraries that got pulled into the binary, which means trying
    # to link several such libraries into the same executable will fail.
    #
    # c2rust-transpile does have proper support for translating all of the
    # libraries and binaries of a C project into separate Rust crates; once our
    # other tools (such as c2rust-refactor) support multi-crate projects, we
    # should switch to that mode and get rid of this hack.

    def is_binary(t):
        return t['type'] == 'EXECUTABLE'
    def is_library(t):
        return t['type'] in ('STATIC_LIBRARY', 'SHARED_LIBRARY')
    num_binaries = sum(1 for t in targets if is_binary(t))
    num_libraries = sum(1 for t in targets if is_library(t))

    # Supported combinations are 1 library and 0 binaries, or 1 binary and any
    # number of libraries.
    if num_binaries + num_libraries > 1:
        assert num_binaries == 1, \
            "this script can't handle a project with " \
            f'{num_binaries} binaries and {num_libraries} libraries'

    def cmake_artifact(name, bin_main = None):
        build_cmd = ['cmake', '--build', 'build']
        if num_binaries + num_libraries > 1:
            # If there are multiple targets, specify a particular one to build.
            #
            # TODO (hack): some of the Test-Corpus examples use the name of the
            # parent directory (above `test_case/`) to set the name of the
            # build target.  This name changes when we copy the code into a
            # sandbox: the original directory `foo/test_case/` gets copied to
            # `/tmp/sandbox/test_case` or similar, and attempting to build
            # `foo` fails because the target is now called `sandbox`.  It
            # happens to be the case that all such projects in `Test-Corpus`
            # have only a single target.  We handle these projects by building
            # without a specific target name, which builds the sole target
            # regardless of its name, while still setting the artifact name in
            # `crisp.toml` to the original name detected by this script, so
            # that the final library or binary will still have the name
            # expected by the test suite.
            build_cmd.extend(('--', name))
        art = {
            'name': name,
            'configure_cmds': shlex.join(
                ['cmake', '-B', 'build', str(cmake_dir_from_project)] + cmake_extra_args),
            'build_cmds': shlex.join(build_cmd),
            # Hack: add -lcrypto, which is required for one test case
            'system_libs': ['crypto'],
        }
        if bin_main is not None:
            art['bin_main'] = bin_main
        return art

    if num_binaries == 1:
        # Transpile the binary first.
        for target in targets:
            if not is_binary(target):
                continue
            art = cmake_artifact(target['name'],
                bin_main = find_file_containing_main(cmake_dir, target))
            cfg_parts.append('[[transpile.artifacts]]\n' + toml.dumps(art))
            bin_name = target['name']

        # Derive libraries from the transpiled binary
        for target in targets:
            if not is_library(target):
                continue
            art = {
                'name': target['name'],
                'lib_from_bin_artifact': bin_name,
            }
            cfg_parts.append('[[transpile.artifacts]]\n' + toml.dumps(art))

    else:
        assert num_binaries == 0
        assert num_libraries == 1
        # Transpile the library
        for target in targets:
            if not is_library(target):
                continue
            art = cmake_artifact(target['name'])
            cfg_parts.append('[[transpile.artifacts]]\n' + toml.dumps(art))

    cfg_str = '\n'.join(cfg_parts)
    (args.project_dir / "crisp.toml").write_text(cfg_str)


    # Collect source files for the project and for test infrastructure.
    commit_paths = [
        cmake_dir,
    ]
    if args.runtests:
        commit_paths.extend((
            base_dir / "Cargo.toml",
        ))
        commit_paths.extend((
            base_dir / "deployment",
            base_dir / "tools",
        ))
    # Add test files for the main project dir and for any extra dirs.
    for test_dir in [args.project_dir] + args.extra_test_dirs:
        commit_paths.extend((
            test_dir / "CMakeLists.txt",
            test_dir / "CMakePresets.json",
        ))
        if args.runtests:
            commit_paths.extend((
                test_dir / "runner",
                test_dir / "test_vectors",
            ))

    commit_excludes = [
        # Compiled/generated output
        'target/', '__pycache__/',
        # CRISP configs and storage
        'crisp*',
        # CRISP outputs that the user may have checked out into the directory
        'translated_rust/', 'compile_commands.json',
    ]
    if not args.runtests:
        commit_excludes.extend((
            # Exclude test runner and test vectors as in official T&E packaging
            # scripts.  This ensures the agent won't stumble upon the tests
            # when running with `--no-runtests` on a checkout that actually
            # does include the tests.
            'runner/', 'test_vectors/',
        ))
    commit_exclude_args = ['--exclude=' + excl for excl in commit_excludes]

    run_crisp(args, ["commit", "-t", "c_code", *commit_exclude_args,
        '--ignore-missing', *commit_paths])
    run_crisp(args, ["main"] + args.main_args)


if __name__ == "__main__":
    main()
