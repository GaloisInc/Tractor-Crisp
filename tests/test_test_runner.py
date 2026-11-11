"""Integration checks for the standalone runner, with real Cargo and C builds."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "crisp_test_runner.py"


@unittest.skipUnless(shutil.which("cargo") and shutil.which("clang"), "requires cargo and clang")
class TestRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="crisp runner ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.c = self.root / "original"
        self.rust = self.root / "rust"
        self.suite = self.root / "suite"
        self.build = self.root / "compiled"
        self.outputs = 0
        self.write(self.c / "include/api.h", "int api(int); int extra(int);\n")
        self.write(self.c / "include/settings.h", "#define BIAS 1\n")
        self.write(self.rust / "Cargo.toml", '''
[package]
name = "example"
version = "0.1.0"
edition = "2021"
[lib]
name = "example_api"
crate-type = ["cdylib"]
[[bin]]
name = "example_cli"
path = "src/main.rs"
[workspace]
members = ["extra"]
''')
        self.write(self.rust / "src/lib.rs", '''
#[no_mangle]
pub extern "C" fn api(x: i32) -> i32 { x * 2 }
''')
        self.write(self.rust / "src/main.rs", '''
use std::io::{self, Read};
fn main() {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input).unwrap();
    print!("{}", input);
    eprintln!("cli diagnostic");
    std::process::exit(7);
}
''')
        self.write(self.rust / "extra/Cargo.toml", '''
[package]
name = "extra"
version = "0.1.0"
edition = "2021"
[lib]
crate-type = ["cdylib"]
''')
        self.write(self.rust / "extra/src/lib.rs", '''
#[no_mangle]
pub extern "C" fn extra(x: i32) -> i32 { x + 5 }
''')
        self.write(self.suite / "tests.toml", 'cflags = ["-Iinclude"]\nldflags = []\n')
        self.write(self.suite / "test_helpers/helpers.c", '''
#include <api.h>
#include <settings.h>
int observe(int x) { return api(x) + extra(x) + BIAS; }
''')
        self.write(self.suite / "test_helpers/helpers.h", "int observe(int);\n")
        self.write(self.suite / "tests/observe.c", '''
// RUN: test_observe
#include <stdio.h>
#include "helpers.h"
int main(void) { printf("%d\\n", observe(3)); return 0; }
''')
        self.write(self.suite / "tests/wrapped.c", '''
#include <stdio.h>
#include <stdlib.h>
int main(void) { printf("%s\\n", getenv("OBSERVATION")); return 0; }
''')
        # These scripts intentionally have no executable bit, like MVIR files.
        self.write(self.suite / "tests/wrapped.sh", '''
test ! -e shared
touch shared
OBSERVATION=wrapped test_wrapped
''')
        self.write(self.suite / "tests/io.py", '''
import os
from pathlib import Path
import subprocess
assert not Path("shared").exists()
Path("shared").write_text("fixture")
assert Path(os.environ["TEST_ROOT"], "fixture.txt").read_text() == "fixture"
p = subprocess.run(["example_cli"], input=b"cli input\\n", capture_output=True)
assert p.returncode == 7
assert p.stderr == b"cli diagnostic\\n"
os.write(1, b"\\x00\\xff" + p.stdout)
os.write(2, b"separate stderr\\x00")
''')
        self.write(self.suite / "tests/error.sh", 'printf "observed error\\n" >&2\nexit 4\n')
        self.write(self.suite / "fixture.txt", "fixture")

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def invoke(self, operation, *extra, success=True):
        args = [sys.executable, "-I", str(RUNNER), operation,
                "--suite-dir", str(self.suite), "--rust-dir", str(self.rust),
                "--build-dir", str(self.build)]
        if operation == "build":
            args.extend(["--c-dir", str(self.c)])
        else:
            self.outputs += 1
            self.output = self.root / f"outputs-{self.outputs}"
            args.extend(["--output-dir", str(self.output)])
        env = os.environ.copy()
        env.pop("CARGO_TARGET_DIR", None)
        p = subprocess.run([*args, *extra], capture_output=True, env=env, timeout=45)
        if success:
            self.assertEqual(p.returncode, 0, (p.stderr + p.stdout).decode(errors="replace"))
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0, p.stdout.decode(errors="replace"))
        return p

    def observations(self, result):
        return {case["name"]: (
            (self.output / case["stdout"]).read_bytes(),
            (self.output / case["stderr"]).read_bytes(), case["returncode"],
        ) for case in result["tests"]}

    def test_multiple_libraries_helpers_scripts_and_binary_streams(self):
        built = self.invoke("build")
        self.assertFalse(built["cached"])
        self.assertEqual(built["programs"], 2)
        result = self.invoke("run")
        self.assertTrue(result["complete"])
        self.assertEqual(self.observations(result), {
            "tests/error.sh": (b"", b"observed error\n", 4),
            "tests/io.py": (b"\x00\xffcli input\n", b"separate stderr\x00", 0),
            "tests/observe.c": (b"15\n", b"", 0),
            "tests/wrapped.sh": (b"wrapped\n", b"", 0),
        })

    def test_cache_invalidation_and_binary_integrity(self):
        self.invoke("build")
        self.assertTrue(self.invoke("build")["cached"])
        self.write(self.c / "include/settings.h", "#define BIAS 2\n")
        self.assertFalse(self.invoke("build")["cached"])
        self.assertEqual(self.observations(self.invoke("run"))["tests/observe.c"][0], b"16\n")
        executable = self.build / "bin/test_observe"
        executable.write_bytes(b"broken executable")
        self.assertFalse(self.invoke("build")["cached"])
        self.write(self.suite / "tests.toml", 'cflags = ["-Iinclude", "-DBUILD_FLAG"]\n')
        self.assertFalse(self.invoke("build")["cached"])

    def test_identical_binaries_against_changed_rust_without_c_headers(self):
        self.invoke("build")
        original_hash = hashlib.sha256((self.build / "bin/test_observe").read_bytes()).digest()
        self.assertEqual(self.observations(self.invoke("run"))["tests/observe.c"][0], b"15\n")
        candidate = self.root / "candidate-rust"
        shutil.copytree(self.rust, candidate, ignore=shutil.ignore_patterns("target"))
        self.rust = candidate
        self.write(self.rust / "src/lib.rs", '''
#[no_mangle]
pub extern "C" fn api(x: i32) -> i32 { x * 3 }
''')
        shutil.rmtree(self.c)
        relocated = self.root / "relocated"
        shutil.move(self.build, relocated)
        self.build = relocated
        for executable in (self.build / "bin").iterdir():
            executable.chmod(0o644)
        result = self.invoke("run")
        self.assertEqual(self.observations(result)["tests/observe.c"][0], b"18\n")
        self.assertEqual(hashlib.sha256((self.build / "bin/test_observe").read_bytes()).digest(),
                         original_hash)

    def test_failed_compile_preserves_previous_bundle_and_run_rejects_stale_sources(self):
        self.invoke("build")
        saved = (self.build / "manifest.json").read_bytes()
        self.write(self.suite / "tests/broken.c", "this is not C;\n")
        self.invoke("build", success=False)
        self.assertEqual((self.build / "manifest.json").read_bytes(), saved)
        failure = self.invoke("run", success=False)
        self.assertIn(b"rebuild the test binaries", failure.stderr)

    def test_script_only_suite(self):
        shutil.rmtree(self.suite / "test_helpers")
        for source in (self.suite / "tests").glob("*.c"):
            source.unlink()
        (self.suite / "tests/wrapped.sh").unlink()
        self.assertEqual(self.invoke("build")["programs"], 0)
        self.assertTrue(self.invoke("run")["complete"])

    def test_per_case_timeout_and_output_limits(self):
        self.write(self.suite / "tests/00_slow.py", "import time\ntime.sleep(20)\n")
        self.invoke("build")
        self.invoke("run", "--test-timeout", "0.1", success=False)
        result = json.loads((self.output / "results.json").read_text())
        self.assertEqual(result["tests"][0]["status"], "timeout")
        self.assertEqual(result["tests"][-1]["status"], "exited")
        (self.suite / "tests/00_slow.py").unlink()
        self.write(self.suite / "tests/00_loud.py", 'import os\nos.write(1, b"x" * 65536)\n')
        self.invoke("run", "--output-limit", "128", success=False)
        result = json.loads((self.output / "results.json").read_text())
        self.assertEqual(result["tests"][0]["status"], "output_limit")
        self.assertEqual((self.output / result["tests"][0]["stdout"]).stat().st_size, 128)

    def test_suite_timeout_marks_unstarted_tests(self):
        self.write(self.suite / "tests/00_slow.py", "import time\ntime.sleep(20)\n")
        self.invoke("build")
        self.invoke("run", "--test-timeout", "2", "--suite-timeout", "0.1", success=False)
        result = json.loads((self.output / "results.json").read_text())
        self.assertEqual(result["tests"][0]["reason"], "suite_timeout")
        self.assertTrue(all(t["status"] == "not_run" for t in result["tests"][1:]))

    def test_background_children_do_not_hold_capture_pipes_open(self):
        self.write(self.suite / "tests/background.sh", "sleep 20 &\nprintf 'done\\n'\n")
        self.invoke("build")
        result = self.invoke("run", "--test-timeout", "2")
        background = next(t for t in result["tests"] if t["name"] == "tests/background.sh")
        self.assertEqual(background["status"], "exited")
        self.assertLess(background["elapsed_seconds"], 2)

    def test_detached_children_are_killed_and_reaped(self):
        self.write(self.suite / "tests/detached.py", '''
import subprocess
p = subprocess.Popen(["sleep", "20"], start_new_session=True)
print(p.pid)
''')
        self.invoke("build")
        result = self.invoke("run", "--test-timeout", "2")
        case = next(t for t in result["tests"] if t["name"] == "tests/detached.py")
        self.assertEqual(case["status"], "exited")
        pid = int((self.output / case["stdout"]).read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_empty_suite_and_multiple_run_lines_are_errors(self):
        self.invoke("build")
        path = self.suite / "tests/observe.c"
        self.write(path, "// RUN: test_observe\n" + path.read_text())
        self.invoke("build")
        self.assertIn(b"one nonempty", self.invoke("run", success=False).stderr)
        self.write(path, "// RUN:\nint main(void) { return 0; }\n")
        self.invoke("build")
        self.assertIn(b"one nonempty", self.invoke("run", success=False).stderr)
        shutil.rmtree(self.suite / "tests")
        (self.suite / "tests").mkdir()
        self.invoke("build")
        self.assertIn(b"no runnable tests", self.invoke("run", success=False).stderr)

    def test_sanitizers_instrument_c_tests(self):
        self.write(self.suite / "tests/undefined.c", '''
// RUN: test_undefined
#include <stdio.h>
int main(void) { int a[1] = {0}; printf("%d", a[1]); return 0; }
''')
        self.invoke("build", "--sanitize")
        result = self.invoke("run")
        case = next(t for t in result["tests"] if t["name"] == "tests/undefined.c")
        self.assertNotEqual(case["returncode"], 0)
        self.assertIn(b"runtime error", (self.output / case["stderr"]).read_bytes())

    def test_direct_library_test_without_helpers_and_signal_reporting(self):
        shutil.rmtree(self.suite / "test_helpers")
        shutil.rmtree(self.suite / "tests")
        self.write(self.suite / "tests/direct.c", '''
// RUN: test_direct
#include <stdio.h>
#include <api.h>
int main(void) { printf("%d\\n", api(4)); return 0; }
''')
        self.invoke("build")
        self.assertEqual(self.observations(self.invoke("run"))["tests/direct.c"][0], b"8\n")
        self.write(self.suite / "tests/terminated.py", "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n")
        self.invoke("run", success=False)
        result = json.loads((self.output / "results.json").read_text())
        case = next(t for t in result["tests"] if t["name"] == "tests/terminated.py")
        self.assertEqual(case["status"], "signaled")
        self.assertEqual(case["returncode"], -15)

    def test_suite_owned_link_flags(self):
        self.write(self.suite / "test_helpers/helpers.c", '''
#include <api.h>
#include <math.h>
int observe(int x) { return api(x) + (int)sin((double)x); }
''')
        self.write(self.suite / "tests.toml", 'cflags = ["-Iinclude"]\nldflags = ["-lm"]\n')
        self.invoke("build")
        self.assertEqual(self.observations(self.invoke("run"))["tests/observe.c"][0], b"6\n")


if __name__ == "__main__":
    unittest.main()
