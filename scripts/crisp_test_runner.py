#!/usr/bin/env python3
"""Build C observation tests and run them against Rust (Python 3.11+, stdlib only).

Run inside a caller-provided sandbox. See docs/test-framework.md for the CLI,
suite conventions and observation format. MVIR manages caching outside this script.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib


VERSION = 1
HELPERS = "libcrisp_test_helpers.so"
RUN_LINE = re.compile(r"^[ \t]*//[ \t]*RUN:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
SUBREAPER = False


class RunnerError(Exception):
    pass


def write_json(path: Path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def children():
    return set(map(int, Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()))


def reap_children(previous):
    # Linux reparents orphaned descendants to this script after PR_SET_CHILD_SUBREAPER.
    # This also catches daemons that escaped the command's process group via setsid.
    while remaining := children() - previous:
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in remaining:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def execute(argv, *, cwd, env=None, timeout=300, output_limit=32 * 1024 * 1024):
    """Capture bounded byte streams and clean up subprocesses, including daemons."""
    started = time.monotonic()
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    status = "exited"
    previous = children() if SUBREAPER else set()
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            umask=0o022,
        )
    except OSError as exc:
        return {"status": "launch_error", "returncode": None,
                "stdout": b"", "stderr": str(exc).encode(), "elapsed_seconds": 0}

    def kill_group():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        with selectors.DefaultSelector() as selector:
            for name in streams:
                pipe = getattr(proc, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map() or proc.poll() is None:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    status = "timeout"
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output = streams[key.data]
                    available = output_limit - len(output)
                    output.extend(chunk[:available])
                    if len(chunk) > available:
                        status = "output_limit"
                        break
                if status != "exited":
                    break
                if proc.poll() is not None:
                    kill_group()
                    if SUBREAPER:
                        reap_children(previous)
    finally:
        # Also terminate children left behind after a successful command.
        kill_group()
        proc.wait()
        if SUBREAPER:
            reap_children(previous)
        proc.stdout.close()
        proc.stderr.close()
    if status == "exited" and proc.returncode < 0:
        status = "signaled"
    return {"status": status, "returncode": proc.returncode,
            **{name: bytes(value) for name, value in streams.items()},
            "elapsed_seconds": time.monotonic() - started}


def checked(argv, *, cwd, timeout):
    print("+ " + shlex.join(map(str, argv)), file=sys.stderr)
    result = execute(argv, cwd=cwd, timeout=timeout)
    if result["stderr"]:
        print(result["stderr"].decode(errors="replace"), end="", file=sys.stderr)
    if result["status"] != "exited" or result["returncode"] != 0:
        print(result["stdout"].decode(errors="replace"), end="", file=sys.stderr)
        raise RunnerError(
            f"command failed ({result['status']}, exit {result['returncode']}): "
            + shlex.join(map(str, argv)))
    return result["stdout"]


def cargo_targets(rust_dir: Path, timeout: float):
    output = checked(
        ["cargo", "build", "--workspace", "--message-format=json"],
        cwd=rust_dir, timeout=timeout,
    )
    libraries, binaries = {}, {}
    for line in output.splitlines():
        try:
            message = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if message.get("reason") == "compiler-message":
            rendered = message["message"].get("rendered")
            if rendered:
                print(rendered, end="", file=sys.stderr)
        if message.get("reason") != "compiler-artifact":
            continue
        if not Path(message["manifest_path"]).resolve().is_relative_to(rust_dir):
            continue
        kind = message["target"]["kind"]
        products = []
        if "cdylib" in kind:
            products.extend((libraries, Path(p)) for p in message["filenames"]
                            if p.endswith(".so"))
        if "bin" in kind and message.get("executable"):
            products.append((binaries, Path(message["executable"])))
        for collection, path in products:
            path = path.resolve()
            if path.name in collection and collection[path.name] != path:
                raise RunnerError(f"duplicate Cargo artifact name: {path.name}")
            collection[path.name] = path
    if not libraries and not binaries:
        raise RunnerError("Cargo produced no project binaries or cdylib libraries")
    if libraries.keys() & binaries.keys():
        raise RunnerError("a Cargo binary and library have the same runtime filename")
    return libraries, binaries


def suite_flags(suite: Path):
    config = suite / "tests.toml"
    flags = tomllib.loads(config.read_text()) if config.exists() else {}
    if flags.keys() - {"cflags", "ldflags"}:
        raise RunnerError("tests.toml supports only cflags and ldflags")
    for name in ("cflags", "ldflags"):
        value = flags.setdefault(name, [])
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise RunnerError(f"tests.toml: {name} must be an array of strings")
    return flags


def suite_sources(suite: Path):
    return sorted((suite / "test_helpers").glob("*.c")), sorted((suite / "tests").glob("*.c"))


def read_manifest(build_dir: Path):
    manifest = json.loads((build_dir / "manifest.json").read_text())
    if manifest["version"] != VERSION:
        raise RunnerError("unsupported compiled-test manifest version")
    for name in manifest["files"]:
        path = Path(name)
        if (not path.parts or path.is_absolute() or ".." in path.parts
                or path.parts[0] not in ("bin", "lib")):
            raise RunnerError(f"invalid compiled-test path: {name}")
    return manifest


def build_tests(args):
    suite, source, destination = args.suite_dir, args.c_dir, args.build_dir
    flags = suite_flags(suite)
    helpers, tests = suite_sources(suite)
    libraries, binaries = cargo_targets(args.rust_dir, args.build_timeout)
    if HELPERS in libraries:
        raise RunnerError(f"target library name is reserved: {HELPERS}")
    if any("test_" + p.stem in binaries for p in tests):
        raise RunnerError("a target binary conflicts with a test_ executable name")
    cc = shutil.which(args.cc)
    if cc is None:
        raise RunnerError(f"C compiler not found: {args.cc}")
    destination.mkdir(parents=True, exist_ok=True)
    instrumentation = (["-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                        "-fno-omit-frame-pointer"] if args.sanitize else [])
    with tempfile.TemporaryDirectory(prefix=".compile-", dir=destination) as temporary:
        staging = Path(temporary)
        (staging / "bin").mkdir()
        (staging / "lib").mkdir()
        target_flags = []
        for directory in sorted({str(p.parent) for p in libraries.values()}):
            target_flags.extend(["-L", directory])
        # Use -l:basename, never an absolute .so link input: Rust libraries may
        # lack a SONAME, which would otherwise embed the build path in DT_NEEDED.
        target_flags.extend("-l:" + name for name in sorted(libraries))
        target_flags.extend("-l" + name for name in args.system_lib)

        def compile_object(path, index, pic=False):
            obj = staging / f"{index}.o"
            checked([cc, *flags["cflags"], *instrumentation,
                     "-I", str(source), "-I", str(suite / "test_helpers"),
                     "-I", str(suite / "tests"), *(["-fPIC"] if pic else []),
                     "-c", str(path), "-o", str(obj)],
                    cwd=source, timeout=args.build_timeout)
            return str(obj)

        helper_objects = [compile_object(p, f"helper-{i}", pic=True)
                          for i, p in enumerate(helpers)]
        if helper_objects:
            checked([cc, "-shared", *helper_objects, *instrumentation,
                     "-Wl,-soname," + HELPERS, "-o", str(staging / "lib" / HELPERS),
                     *target_flags, *flags["ldflags"]],
                    cwd=source, timeout=args.build_timeout)
        for i, path in enumerate(tests):
            obj = compile_object(path, f"test-{i}")
            checked([cc, obj, *instrumentation,
                     "-o", str(staging / "bin" / ("test_" + path.stem)),
                     *(["-L", str(staging / "lib"), "-l:" + HELPERS]
                       if helper_objects else []), *target_flags, *flags["ldflags"]],
                    cwd=source, timeout=args.build_timeout)
        files = sorted(str(p.relative_to(staging))
                       for directory in ("bin", "lib") for p in (staging / directory).iterdir())
        manifest = {"version": VERSION, "files": files, "sanitize": args.sanitize}
        for name in files:
            path = destination / name
            path.parent.mkdir(exist_ok=True)
            os.replace(staging / name, path)
        write_json(staging / "manifest.json", manifest)
        os.replace(staging / "manifest.json", destination / "manifest.json")
    return {"build_dir": str(destination), "programs": len(tests), "helpers": bool(helpers)}


def discover_tests(suite: Path):
    cases = []
    for path in sorted((suite / "tests").iterdir()):
        if not path.is_file():
            continue
        name = "tests/" + path.name
        if path.suffix == ".c":
            commands = RUN_LINE.findall(path.read_text())
            if len(commands) > 1 or (commands and not commands[0]):
                raise RunnerError(f"{name}: expected at most one nonempty // RUN: command")
            if commands:
                cases.append((name, ["bash", "-o", "pipefail", "-c", commands[0]]))
        elif path.suffix in (".sh", ".py"):
            # MVIR snapshots currently omit executable permissions.
            interpreter = ["bash", "-o", "pipefail"] if path.suffix == ".sh" else ["python3"]
            cases.append((name, [*interpreter, str(path)]))
        elif os.access(path, os.X_OK):
            cases.append((name, [str(path)]))
    if not cases:
        raise RunnerError("no runnable tests found under tests/")
    return cases


def run_tests(args):
    suite, compiled = args.suite_dir, args.build_dir
    manifest = read_manifest(compiled)
    cases = discover_tests(suite)
    libraries, binaries = cargo_targets(args.rust_dir, args.build_timeout)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise RunnerError("--output-dir must be empty (use separate directories for snapshots)")

    results = []
    with tempfile.TemporaryDirectory(prefix="crisp-runtime-") as temporary:
        runtime = Path(temporary)
        for directory in ("targets", "bin", "lib"):
            (runtime / directory).mkdir()
        for name, path in {**libraries, **binaries}.items():
            staged = runtime / "targets" / name
            shutil.copyfile(path, staged)
            staged.chmod(0o755)
        for name in manifest["files"]:
            staged = runtime / name
            shutil.copyfile(compiled / name, staged)
            staged.chmod(0o755 if name.startswith("bin/") else 0o644)
        env = os.environ.copy()
        env.update({"PATH": os.pathsep.join([str(runtime / "bin"), str(runtime / "targets"),
                                             env.get("PATH", os.defpath)]),
                    "LD_LIBRARY_PATH": os.pathsep.join([str(runtime / "lib"),
                                                         str(runtime / "targets")]),
                    "LD_BIND_NOW": "1", "TEST_ROOT": str(suite),
                    "TEST_BINS": str(runtime / "bin"), "LC_ALL": "C", "TZ": "UTC"})
        for name in ("LD_PRELOAD", "LD_AUDIT"):
            env.pop(name, None)
        if manifest["sanitize"]:
            # External symbolizers can block in a restricted sandbox. Keep raw
            # stack addresses and UBSan's built-in source locations instead.
            env["ASAN_OPTIONS"] = "halt_on_error=1:detect_leaks=1:symbolize=0"
            env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1:symbolize=0"
        deadline = time.monotonic() + args.suite_timeout
        for index, (name, argv) in enumerate(cases):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result = {"status": "not_run", "returncode": None,
                          "reason": "suite_timeout", "stdout": b"", "stderr": b"",
                          "elapsed_seconds": 0}
            else:
                with tempfile.TemporaryDirectory(prefix="crisp-test-") as work:
                    work = Path(work)
                    (work / "home").mkdir()
                    (work / "tmp").mkdir()
                    result = execute(
                        argv, cwd=work, env={**env, "HOME": str(work / "home"),
                                            "TMPDIR": str(work / "tmp")},
                        timeout=min(args.test_timeout, remaining), output_limit=args.output_limit)
                    if result["status"] == "timeout" and remaining <= args.test_timeout:
                        result["reason"] = "suite_timeout"
            for stream in ("stdout", "stderr"):
                contents = result.pop(stream)
                filename = f"{index:04d}.{stream}"
                (output / filename).write_bytes(contents)
                result[stream] = filename
            results.append({"name": name, **result})
    summary = {"version": VERSION, "tests": results,
               "complete": all(r["status"] == "exited" for r in results)}
    write_json(output / "results.json", summary)
    return summary


def positive_float(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return value


def positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def main(argv=None):
    global SUBREAPER
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("build", "run"):
        command = subcommands.add_parser(operation)
        command.add_argument("--suite-dir", type=Path, required=True)
        command.add_argument("--rust-dir", type=Path, required=True)
        command.add_argument("--build-dir", type=Path, required=True)
        command.add_argument("--build-timeout", type=positive_float, default=300)
        if operation == "build":
            command.add_argument("--c-dir", type=Path, required=True)
            command.add_argument("--cc", default="clang")
            command.add_argument("--system-lib", action="append", default=[])
            command.add_argument("--sanitize", action="store_true")
        else:
            command.add_argument("--output-dir", type=Path, required=True)
            command.add_argument("--test-timeout", type=positive_float, default=5)
            command.add_argument("--suite-timeout", type=positive_float, default=30)
            command.add_argument("--output-limit", type=positive_int, default=1024 * 1024)
    args = parser.parse_args(argv)
    for name in ("suite_dir", "rust_dir", "build_dir", "c_dir", "output_dir"):
        if hasattr(args, name):
            setattr(args, name, getattr(args, name).resolve())
    try:
        if sys.platform != "linux":
            raise RunnerError("this runner requires Linux")
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise RunnerError("cannot enable Linux child-subreaper cleanup")
        SUBREAPER = True
        result = build_tests(args) if args.operation == "build" else run_tests(args)
    except (RunnerError, OSError, ValueError, KeyError) as exc:
        print(f"crisp_test_runner: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if args.operation == "build" or result["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
