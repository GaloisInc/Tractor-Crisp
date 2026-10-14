"""
Generate test cases for parts of the C code using an LLM.

The LLM only writes test *inputs* (C test drivers or command-line arguments and
stdin); it never writes expected outputs.  Instead, each test is compiled and
run against the original C code in the sandbox, and the behavior observed
there (stdout and exit code) is recorded as the expected output.  Tests that
fail to build, crash, time out, or behave differently on two runs are
rejected, and their errors are fed back to the LLM for repair.

Usage, from the project directory after importing the C code with
`crisp commit -t c_code ...`:

    uv run --project $CRISP_DIR python -m crisp.gen_tests \\
        src/parse.c:parse_header src/util.c:10-40 encode_frame

Each `FRAGMENT` argument selects part of the C code to include in the prompt:

* `FILE`: the whole file
* `FILE:START-END` or `FILE:LINE`: a range of lines (1-based, inclusive)
* `FILE:FUNC`: the definition of function `FUNC` in `FILE`
* `FUNC`: the definition of function `FUNC`, wherever it is

The accepted tests are stored as a `TreeNode` tagged `generated_tests`, and a
record of the run (prompt, LLM requests and responses, observations) is tagged
`gen_tests_record`.  Use `crisp checkout generated_tests` to write the tests to
disk.  Each test is a directory under `--output-dir` containing `driver.c`
(driver mode) or `args.json` (cli mode), an optional `stdin`, and the observed
`expected_stdout` and `expected_exit_code`.

The prompts follow the characterisation-test conventions of the relational
mutation testing baseline (`rmt/prompts/probe.md`): driver-mode tests record
results with `obs_ll`/`obs_dbl`/`obs_str` (defined in `crisp_obs.h`, which is
force-included when building drivers and shipped with the tests) under unique
hierarchical labels, rather than asserting values the LLM worked out itself.
"""

import argparse
from dataclasses import dataclass, asdict
import fnmatch
import json
import os
from pathlib import Path
import re
import shlex
import sys

from . import llm, prompts
from .config import Config, TranspileArtifactConfig
from .error import CrispError
from .mvir import MVIR, FileNode, TreeNode
from .sandbox import run_sandbox


TESTS_TAG = 'generated_tests'
RECORD_TAG = 'gen_tests_record'

# Standalone script, shipped with the tests as `run_tests.py`, that replays
# them against the Rust translation.
RUNNER_NAME = 'run_tests.py'
RUNNER_SOURCE = (Path(__file__).parent / 'gen_tests_runner.py').read_text()

# Scratch directory inside the sandbox.
SCRATCH_DIR = '.crisp_gen_tests'

C_SOURCE_SUFFIXES = ('.c',)
C_HEADER_SUFFIXES = ('.h',)


@dataclass(frozen = True)
class GenTestsSettings:
    mode: str
    num_tests: int = 10
    # `cli` mode: command (run from the project directory) that runs the
    # program; test arguments are appended.
    run_cmd: str | None = None
    # `driver` mode: compiler, flags, and glob patterns of C sources to leave
    # out when linking test drivers.
    cc: str = 'cc'
    cflags: tuple[str, ...] = ('-std=gnu11', '-O0', '-g', '-w')
    ldflags: tuple[str, ...] = ('-lm',)
    exclude_sources: tuple[str, ...] = ()
    timeout: int = 10
    max_output_bytes: int = 65536
    repair_rounds: int = 3
    # How many times to re-ask when the LLM reply is empty or contains no
    # usable tests.  An empty reply is a budget or transport symptom, not a
    # verdict on the code.
    empty_retries: int = 2
    # `driver` mode: bounds on the number of values each test must record.
    min_records: int = 1
    max_records: int = 600
    # Seconds to wait between the two runs of each test, so tests that depend
    # on the clock behave differently and are rejected as nondeterministic.
    rerun_delay: float = 1.1
    context_headers: bool = True
    stream: bool = True
    max_context_chars: int = 60000
    extra_instructions: str | None = None


# Fragment selection

@dataclass(frozen = True)
class FragmentSpec:
    # Path as given by the user, or `None` to search all files for `function`.
    path: str | None
    lines: tuple[int, int] | None = None
    function: str | None = None

@dataclass(frozen = True)
class CFragment:
    # Path within the `c_code` tree
    path: str
    # 1-based, inclusive
    start_line: int
    end_line: int
    text: str
    function: str | None = None

    def describe(self) -> str:
        desc = f'`{self.path}`, lines {self.start_line}-{self.end_line}'
        if self.function is not None:
            desc += f' (function `{self.function}`)'
        return desc


RE_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
RE_LINES = re.compile(r'^(\d+)(?:-(\d+))?$')

def parse_fragment_spec(s: str) -> FragmentSpec:
    if ':' in s:
        path, rest = s.rsplit(':', 1)
        if (m := RE_LINES.match(rest)):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) is not None else start
            if start < 1 or end < start:
                raise CrispError(f'bad line range in fragment {s!r}')
            return FragmentSpec(path, lines = (start, end))
        if RE_IDENT.match(rest):
            return FragmentSpec(path, function = rest)
        raise CrispError(f'bad fragment {s!r}: expected FILE:START-END or FILE:FUNC')
    if RE_IDENT.match(s):
        # Could also be a file named e.g. `Makefile`; `extract_fragments`
        # checks for that.
        return FragmentSpec(None, function = s)
    return FragmentSpec(s)


def mask_c_source(src: str) -> str:
    """
    Replace comments, string and character literals, and preprocessor
    directives in `src` with spaces, preserving newlines and character
    offsets.  This makes it safe to match braces and parentheses.
    """
    out = list(src)
    n = len(src)

    def blank(start, end):
        for k in range(start, end):
            if out[k] != '\n':
                out[k] = ' '

    i = 0
    at_line_start = True
    while i < n:
        c = src[i]
        if c == '\n':
            at_line_start = True
            i += 1
        elif c in ' \t\r\f\v':
            i += 1
        elif c == '#' and at_line_start:
            j = i
            while j < n and not (src[j] == '\n' and src[j - 1] != '\\'):
                j += 1
            blank(i, j)
            i = j
        elif src.startswith('//', i):
            j = src.find('\n', i)
            j = n if j == -1 else j
            blank(i, j)
            i = j
        elif src.startswith('/*', i):
            j = src.find('*/', i + 2)
            j = n if j == -1 else j + 2
            blank(i, j)
            i = j
            at_line_start = False
        elif c in '"\'':
            j = i + 1
            while j < n and src[j] != c and src[j] != '\n':
                j += 2 if src[j] == '\\' else 1
            j = min(j + 1, n)
            blank(i, j)
            i = j
            at_line_start = False
        else:
            at_line_start = False
            i += 1
    return ''.join(out)


def _match_close(masked: str, open_pos: int, open_ch: str, close_ch: str) -> int | None:
    depth = 0
    for k in range(open_pos, len(masked)):
        if masked[k] == open_ch:
            depth += 1
        elif masked[k] == close_ch:
            depth -= 1
            if depth == 0:
                return k
    return None

def _is_comment_line(line: str) -> bool:
    s = line.strip()
    return s.startswith(('//', '/*', '*')) or s.endswith('*/')

def find_function_lines(src: str, name: str) -> tuple[int, int] | None:
    """
    Find the definition of function `name` in C source `src`.  Returns the
    1-based, inclusive line range of the definition, including any comment
    immediately above it, or `None` if there is no definition.  Prototypes
    and calls are ignored.
    """
    masked = mask_c_source(src)
    pattern = re.compile(r'(?<![\w.>])' + re.escape(name) + r'\s*\(')

    depth = 0
    pos = 0
    for m in pattern.finditer(masked):
        # Track brace depth up to the match; only top-level names can be
        # function definitions.
        chunk = masked[pos:m.start()]
        depth += chunk.count('{') - chunk.count('}')
        pos = m.start()
        if depth != 0:
            continue

        close_paren = _match_close(masked, m.end() - 1, '(', ')')
        if close_paren is None:
            continue
        k = close_paren + 1
        while k < len(masked) and masked[k].isspace():
            k += 1
        if k >= len(masked) or masked[k] != '{':
            continue
        close_brace = _match_close(masked, k, '{', '}')
        if close_brace is None:
            continue

        # The definition starts after the previous top-level declaration.
        start = max(masked.rfind(';', 0, m.start()), masked.rfind('}', 0, m.start())) + 1
        while start < m.start() and masked[start].isspace():
            start += 1

        lines = src.splitlines()
        start_line = src.count('\n', 0, start) + 1
        end_line = src.count('\n', 0, close_brace) + 1
        while start_line > 1 and _is_comment_line(lines[start_line - 2]):
            start_line -= 1
        return start_line, end_line
    return None


def _lines_text(src: str, start_line: int, end_line: int) -> str:
    lines = src.splitlines(keepends = True)
    return ''.join(lines[start_line - 1 : end_line])

def _resolve_path(tree_paths, path: str, cwd_rel: str) -> str:
    """
    Map a user-provided `path` (relative to the current directory) to a path
    in the `c_code` tree.
    """
    candidates = [os.path.normpath(os.path.join(cwd_rel, path)), os.path.normpath(path)]
    for c in candidates:
        if c in tree_paths:
            return c
    suffix_matches = sorted(p for p in tree_paths if p.endswith('/' + path))
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    if len(suffix_matches) > 1:
        raise CrispError(f'path {path!r} is ambiguous: matches {", ".join(suffix_matches)}')
    raise CrispError(f'file {path!r} not found in c_code')

def extract_fragments(
    mvir: MVIR,
    n_c_code: TreeNode,
    specs: list[FragmentSpec],
    cwd_rel: str = '.',
) -> list[CFragment]:
    files = n_c_code.files
    def src_of(path):
        return mvir.node(files[path]).body_str()

    fragments = []
    for spec in specs:
        if spec.path is None:
            # Bare identifier: a file name if one matches, else a function.
            try:
                path = _resolve_path(files, spec.function, cwd_rel)
            except CrispError:
                path = None
            if path is not None:
                spec = FragmentSpec(spec.function)
            else:
                found = []
                for p in sorted(files):
                    if not p.endswith(C_SOURCE_SUFFIXES + C_HEADER_SUFFIXES):
                        continue
                    src = src_of(p)
                    if (r := find_function_lines(src, spec.function)) is not None:
                        found.append(CFragment(p, r[0], r[1],
                            _lines_text(src, *r), spec.function))
                if len(found) == 0:
                    raise CrispError(f'no definition of function {spec.function!r} found in c_code')
                if len(found) > 1:
                    raise CrispError(f'function {spec.function!r} is defined in multiple files: '
                        + ', '.join(f.path for f in found)
                        + '; use FILE:FUNC to pick one')
                fragments.append(found[0])
                continue

        path = _resolve_path(files, spec.path, cwd_rel)
        src = src_of(path)
        num_lines = len(src.splitlines())
        if spec.function is not None:
            r = find_function_lines(src, spec.function)
            if r is None:
                raise CrispError(f'no definition of function {spec.function!r} found in {path}')
            fragments.append(CFragment(path, r[0], r[1], _lines_text(src, *r), spec.function))
        elif spec.lines is not None:
            start, end = spec.lines
            if start > num_lines:
                raise CrispError(f'{path} has only {num_lines} lines')
            end = min(end, num_lines)
            fragments.append(CFragment(path, start, end, _lines_text(src, start, end)))
        else:
            fragments.append(CFragment(path, 1, max(num_lines, 1), src))

    # Drop exact duplicates, keeping order.
    return list(dict.fromkeys(fragments))


RE_INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.MULTILINE)

def find_included_headers(mvir: MVIR, n_c_code: TreeNode, path: str) -> list[str]:
    """
    Find the tree paths of headers included with `#include "..."` from `path`.
    """
    files = n_c_code.files
    src = mvir.node(files[path]).body_str()
    headers = []
    for inc in RE_INCLUDE.findall(src):
        direct = os.path.normpath(os.path.join(os.path.dirname(path), inc))
        if direct in files:
            headers.append(direct)
            continue
        matches = sorted(p for p in files if p == inc or p.endswith('/' + inc))
        if matches:
            headers.append(matches[0])
    return headers


# Prompt construction

def _format_fragment(frag: CFragment) -> str:
    return f'{frag.describe()}:\n\n```c\n{frag.text.rstrip()}\n```'

def build_context(
    mvir: MVIR,
    n_c_code: TreeNode,
    fragments: list[CFragment],
    settings: GenTestsSettings,
) -> str:
    if not settings.context_headers:
        return 'No additional context is provided.'

    whole_files = {f.path for f in fragments
        if f.start_line == 1 and f.text == mvir.node(n_c_code.files[f.path]).body_str()}
    headers = []
    for path in dict.fromkeys(f.path for f in fragments):
        for h in find_included_headers(mvir, n_c_code, path):
            if h not in whole_files and h not in headers:
                headers.append(h)

    parts = []
    omitted = []
    total = 0
    for h in headers:
        text = mvir.node(n_c_code.files[h]).body_str()
        if total + len(text) > settings.max_context_chars:
            omitted.append(h)
            continue
        total += len(text)
        parts.append(f'`{h}`:\n\n```c\n{text.rstrip()}\n```')
    if omitted:
        parts.append('These headers are also included, but were omitted for length: '
            + ', '.join(f'`{h}`' for h in omitted))
    if not parts:
        return 'No additional context is provided.'
    return 'Headers included by the code under test:\n\n' + '\n\n'.join(parts)


@dataclass(frozen = True)
class DriverBuild:
    """How test drivers are compiled and linked against the project."""
    sources: tuple[str, ...]
    excluded: tuple[str, ...]
    include_dirs: tuple[str, ...]
    libs: tuple[str, ...]

def plan_driver_build(
    mvir: MVIR,
    n_c_code: TreeNode,
    settings: GenTestsSettings,
    art: TranspileArtifactConfig | None,
) -> DriverBuild:
    files = n_c_code.files
    sources, excluded = [], []
    for p in sorted(files):
        if not p.endswith(C_SOURCE_SUFFIXES):
            continue
        stem = os.path.splitext(os.path.basename(p))[0]
        if any(fnmatch.fnmatch(p, g) for g in settings.exclude_sources) \
                or (art is not None and art.bin_main == stem) \
                or find_function_lines(mvir.node(files[p]).body_str(), 'main') is not None:
            # The driver provides its own `main`.
            excluded.append(p)
        else:
            sources.append(p)
    # Include the root first, so headers can be included by the paths shown
    # in the prompt.
    include_dirs = ['.'] + sorted({os.path.dirname(p) for p in files
        if p.endswith(C_SOURCE_SUFFIXES + C_HEADER_SUFFIXES)} - {''})
    libs = tuple(art.system_libs) if art is not None else ()
    return DriverBuild(tuple(sources), tuple(excluded), tuple(include_dirs), libs)

def fill_prompt(template: str, **subs) -> str:
    """
    Substitute `<<NAME>>` placeholders in `template`.  Plain replacement
    rather than `str.format`, since the substituted values and the templates
    themselves contain C code, which is full of braces.
    """
    for k, v in subs.items():
        template = template.replace(f'<<{k.upper()}>>', str(v))
    leftover = re.findall(r'<<[A-Z_]+>>', template)
    assert not leftover, f'unfilled prompt placeholders: {leftover}'
    return template

def driver_build_cmd(settings: GenTestsSettings, driver_build: DriverBuild,
        test_path: str = 'test.c', out_path: str = 'test') -> str:
    """The command used to build one test driver, as shown to the LLM."""
    q = shlex.quote
    sources = driver_build.sources
    if len(sources) > 8:
        sources_str = f'<{len(sources)} project sources>'
    else:
        sources_str = ' '.join(q(p) for p in sources)
    return ' '.join([q(settings.cc), *map(q, settings.cflags),
        *('-I' + q(d) for d in driver_build.include_dirs),
        q(test_path), sources_str, '-o', q(out_path),
        *map(q, settings.ldflags), *(q(f'-l{l}') for l in driver_build.libs)])

def build_prompt(
    mvir: MVIR,
    n_c_code: TreeNode,
    fragments: list[CFragment],
    settings: GenTestsSettings,
    driver_build: DriverBuild | None,
    project: str = 'project',
) -> str:
    extra = ''
    if settings.extra_instructions:
        extra = f'\n## Additional instructions\n\n{settings.extra_instructions.strip()}\n'

    common = dict(
        project = project,
        num_tests = settings.num_tests,
        timeout = settings.timeout,
        file_list = '\n'.join(f'- `{p}`' for p in sorted(n_c_code.files)),
        context = build_context(mvir, n_c_code, fragments, settings),
        fragments = '\n\n'.join(_format_fragment(f) for f in fragments),
        extra_instructions = extra,
    )

    if settings.mode == 'driver':
        assert driver_build is not None
        headers = [h for f in fragments for h in find_included_headers(mvir, n_c_code, f.path)]
        headers += sorted(p for p in n_c_code.files if p.endswith(C_HEADER_SUFFIXES))
        return fill_prompt(prompts.GEN_TESTS_DRIVER,
            header_example = headers[0] if headers else 'foo.h',
            min_records = settings.min_records,
            max_records = settings.max_records,
            build_cmd = driver_build_cmd(settings, driver_build),
            **common)
    else:
        return fill_prompt(prompts.GEN_TESTS_CLI, run_cmd = settings.run_cmd, **common)


# Parsing LLM output

@dataclass(frozen = True)
class TestCase:
    name: str
    description: str
    driver: str | None = None
    args: tuple[str, ...] | None = None
    stdin: str = ''

RE_TEST = re.compile(r'<test\s+name="([^"]*)"\s*>(.*?)</test>', re.DOTALL)
RE_CODE_FENCE = re.compile(r'^\s*```[A-Za-z]*\n(.*?)\n```\s*$', re.DOTALL)

def _element(body: str, tag: str) -> str | None:
    m = re.search(rf'<{tag}>(.*?)</{tag}>', body, re.DOTALL)
    return m.group(1) if m is not None else None

def _strip_one_newline(s: str) -> str:
    return s[1:] if s.startswith('\n') else s

def sanitize_name(name: str) -> str:
    name = re.sub(r'[^A-Za-z0-9_]+', '_', name).strip('_').lower()
    return name or 'test'

def parse_tests(text: str, mode: str) -> tuple[list[TestCase], list[str]]:
    """
    Parse test cases from the LLM's response.  Returns the tests and a list of
    warnings about malformed tests that were skipped.
    """
    tests, warnings = [], []
    for m in RE_TEST.finditer(text):
        name = sanitize_name(m.group(1))
        body = m.group(2)
        description = (_element(body, 'description') or '').strip()
        stdin = _element(body, 'stdin')
        stdin = _strip_one_newline(stdin) if stdin is not None else ''

        if mode == 'driver':
            driver = _element(body, 'driver')
            if driver is None or not driver.strip():
                warnings.append(f'test {name!r} has no <driver>')
                continue
            if (fence := RE_CODE_FENCE.match(driver)) is not None:
                driver = fence.group(1)
            driver = _strip_one_newline(driver).rstrip() + '\n'
            tests.append(TestCase(name, description, driver = driver, stdin = stdin))
        else:
            args_text = _element(body, 'args')
            try:
                args = json.loads(args_text) if args_text is not None and args_text.strip() else []
            except json.JSONDecodeError as e:
                warnings.append(f'test {name!r} has invalid <args>: {e}')
                continue
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                warnings.append(f'test {name!r}: <args> must be a JSON array of strings')
                continue
            tests.append(TestCase(name, description, args = tuple(args), stdin = stdin))
    return tests, warnings

RE_DROP = re.compile(r'<drop\s+name="([^"]*)"\s*>(.*?)</drop>', re.DOTALL)

def parse_drops(text: str) -> dict[str, str]:
    """
    Parse `<drop name="...">reason</drop>` elements, with which the LLM gives
    up on a rejected test that cannot be fixed.  Returns names mapped to reasons.
    """
    return {sanitize_name(m.group(1)): m.group(2).strip() for m in RE_DROP.finditer(text)}

def dedupe_names(tests: list[TestCase], taken: set[str] = frozenset()) -> list[TestCase]:
    seen = set(taken)
    out = []
    for t in tests:
        name = t.name
        i = 2
        while name in seen:
            name = f'{t.name}_{i}'
            i += 1
        seen.add(name)
        out.append(TestCase(name, t.description, t.driver, t.args, t.stdin))
    return out


# Running tests against the C code

@dataclass(frozen = True)
class Observation:
    build_failed: bool
    build_log: bytes
    stdout: tuple[bytes | None, bytes | None]
    stderr: tuple[bytes | None, bytes | None]
    exit_code: tuple[int | None, int | None]
    # Global symbols the driver defines that libc or the project also define.
    interposed: tuple[str, ...] = ()

@dataclass(frozen = True)
class Verdict:
    status: str
    reason: str = ''

    @property
    def ok(self):
        return self.status == 'ok'

def _tail(b: bytes | None, limit: int = 4000) -> str:
    s = (b or b'').decode('utf-8', errors = 'replace')
    return s if len(s) <= limit else '...\n' + s[-limit:]

# Recording functions available to driver-mode tests.  Each record is printed
# to stdout as one `label = value` line.
OBS_HEADER_NAME = 'crisp_obs.h'
OBS_HEADER = r"""/* Recording functions for CRISP characterisation tests. */
#include <stdio.h>
#include <string.h>

/* Line-buffer stdout, so records interleave with the library's own output in
   the same order whether the library is the C original or its Rust
   translation (Rust's stdout is line-buffered). */
__attribute__((constructor)) static void obs_init(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
}

static void obs_ll(const char *label, long long v) {
    printf("%s = %lld\n", label, v);
}

/* Doubles always contain `.`, `e`, `inf` or `nan`, which marks them as
   floating point so comparisons can allow for rounding differences. */
static void obs_dbl(const char *label, double v) {
    char buf[64];
    snprintf(buf, sizeof buf, "%.17g", v);
    if (!strpbrk(buf, ".ein")) strcat(buf, ".0");
    printf("%s = %s\n", label, buf);
}

static void obs_str(const char *label, const char *s) {
    if (s == NULL) {
        printf("%s = NULL\n", label);
        return;
    }
    printf("%s = \"", label);
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') printf("\\%c", c);
        else if (c >= 0x20 && c < 0x7f) putchar(c);
        else printf("\\x%02x", c);
    }
    printf("\"\n");
}
"""

RE_RECORD = re.compile(rb'^([!-~]+) = (.*)$')

def check_records(stdout: bytes, min_records: int = 1, max_records: int | None = None) -> str | None:
    """
    Check that `stdout` contains between `min_records` and `max_records`
    records, as produced by the `OBS_HEADER` functions, and that no label is
    recorded twice.  Returns a description of the problem, or `None` if the
    output is valid.  Other lines are allowed, since the library itself may
    print to stdout.
    """
    seen = set()
    for line in stdout.splitlines():
        if (m := RE_RECORD.match(line)) is None:
            continue
        label = m.group(1).decode('ascii')
        if label in seen:
            return f'label {label!r} was recorded more than once; labels must be unique'
        seen.add(label)
    if not seen:
        return 'the test recorded no values; record results with obs_ll, obs_dbl and obs_str'
    if len(seen) < min_records:
        return f'the test recorded only {len(seen)} values; record at least {min_records} ' \
            '(broaden the workload: more inputs, sizes and intermediate results)'
    if max_records is not None and len(seen) > max_records:
        return f'the test recorded {len(seen)} values; record at most {max_records}'
    return None


# Static checks on driver source, applied before building.  These mirror the
# static gates of the relational mutation testing baseline (stage2_testgen).
RE_INCLUDE_C_FILE = re.compile(r'^\s*#\s*include\s*[<"][^>"]*\.c[>"]', re.MULTILINE)
RE_REDIRECT = re.compile(r'\b(dup|dup2|dup3|freopen|setvbuf|setbuf|STDOUT_FILENO)\b')
RE_DEFINES_OBS = re.compile(r'\bobs_(ll|dbl|str|init)\s*\([^;{]*\)\s*\{')

def static_check(test: TestCase, focus_functions: list[str]) -> Verdict | None:
    """
    Check a driver's source for patterns that make it an invalid test.
    Returns a rejection `Verdict`, or `None` if the source is acceptable.
    `focus_functions` are the non-`static` functions selected as fragments;
    the driver must call at least one of them.
    """
    if test.driver is None:
        return None
    src = test.driver
    masked = mask_c_source(src)
    if RE_INCLUDE_C_FILE.search(src):
        return Verdict('includes_c_file', 'the test #includes a .c file; use only the '
            'library\'s headers and API')
    if (m := RE_REDIRECT.search(masked)):
        return Verdict('redirects_output', f'the test uses `{m.group(1)}`; never capture, '
            'redirect or re-buffer stdout/stderr')
    if RE_DEFINES_OBS.search(masked):
        return Verdict('defines_obs', 'the test defines obs_ll/obs_dbl/obs_str; they are '
            'already provided, so only call them')
    if focus_functions and not any(re.search(r'(?<![\w.>])' + re.escape(f) + r'\s*\(', masked)
            for f in focus_functions):
        return Verdict('misses_focus', 'the test never calls the code under test ('
            + ', '.join(f'`{f}`' for f in focus_functions) + ')')
    return None

def focus_functions(fragments: list[CFragment]) -> list[str]:
    """Selected functions a driver can call directly (non-`static`)."""
    return [f.function for f in fragments
        if f.function is not None and not re.match(r'\s*static\b', mask_c_source(f.text))]

def classify(obs: Observation, timeout: int, max_output_bytes: int,
        mode: str = 'cli', min_records: int = 1, max_records: int | None = None) -> Verdict:
    if obs.interposed:
        # A replacement for a libc or library function would be used instead
        # of the translation's when the driver is linked against Rust.
        return Verdict('interposes', 'the test defines functions that libc or the library '
            'already provides: ' + ', '.join(f'`{s}`' for s in obs.interposed)
            + '; never define, wrap or replace them (make your own helpers `static`)')
    if obs.build_failed:
        return Verdict('build_failed', _tail(obs.build_log))
    if None in obs.exit_code or None in obs.stdout:
        return Verdict('error', 'the test did not run')
    code = obs.exit_code[0]
    if code in (124, 137):
        return Verdict('timeout', f'the test did not finish within {timeout} seconds')
    if code in (125, 126, 127):
        return Verdict('run_error', f'the test could not be run (exit code {code}):\n'
            + _tail(obs.stderr[0]))
    if code > 128:
        return Verdict('crashed', f'the test was killed by signal {code - 128}; stderr:\n'
            + _tail(obs.stderr[0]))
    if len(obs.stdout[0]) > max_output_bytes:
        return Verdict('output_too_large',
            f'the test printed {len(obs.stdout[0])} bytes; the limit is {max_output_bytes}')
    if obs.stdout[0] != obs.stdout[1] or obs.exit_code[0] != obs.exit_code[1]:
        return Verdict('nondeterministic',
            'two runs of the test produced different output or exit codes')
    if mode == 'driver':
        # Driver-mode tests record values rather than signaling with their
        # exit status, so any failure means the workload didn't complete.
        if code != 0:
            return Verdict('nonzero_exit', f'main returned {code}; tests must exit 0. stderr:\n'
                + _tail(obs.stderr[0]))
        if (problem := check_records(obs.stdout[0], min_records, max_records)) is not None:
            return Verdict('bad_records', problem)
    return Verdict('ok')


RESULT_FILES = ('build.log', 'build_failed', 'out1', 'out2', 'err1', 'err2', 'status1', 'status2',
    'syms')
# Symbols defined by libc/libm and by the project objects, for the
# interposition check.
SYMBOL_FILES = ('libc_syms', 'project_syms')
NM_DEFINED = "nm -g --defined-only {} 2>/dev/null | awk 'NF == 3 {{ sub(/@.*/, \"\", $3); print $3 }}'"

def _run_script(
    tests: list[TestCase],
    settings: GenTestsSettings,
    driver_build: DriverBuild | None,
    cwd_rel: str,
) -> str:
    q = shlex.quote
    t = int(settings.timeout)
    delay = f'sleep {settings.rerun_delay}' if settings.rerun_delay > 0 else 'true'
    lines = ['set -u', 'ROOT=$(pwd)']
    if settings.mode == 'driver':
        incs = ' '.join('-I' + q(d) for d in driver_build.include_dirs)
        incs += ' -include ' + q(f'{SCRATCH_DIR}/{OBS_HEADER_NAME}')
        objs = ' '.join(q(f'{SCRATCH_DIR}/obj/{i}.o') for i in range(len(driver_build.sources)))
        cflags = ' '.join(q(f) for f in settings.cflags)
        ldflags = ' '.join([q(f) for f in settings.ldflags] + [q(f'-l{l}') for l in driver_build.libs])
        for test in tests:
            d = q(f'{SCRATCH_DIR}/run/{test.name}')
            lines += [
                f'if {q(settings.cc)} {cflags} {incs} -c {d}/driver.c -o {d}/driver.o'
                    f' > {d}/build.log 2>&1 && {{ {NM_DEFINED.format(f"{d}/driver.o")} > {d}/syms; true; }}'
                    f' && {q(settings.cc)} {cflags} {d}/driver.o {objs} -o {d}/test {ldflags}'
                    f' >> {d}/build.log 2>&1; then',
                f'  (cd {d} && timeout -k 1 {t} ./test < stdin > out1 2> err1; echo $? > status1)',
                f'  {delay}',
                f'  (cd {d} && timeout -k 1 {t} ./test < stdin > out2 2> err2; echo $? > status2)',
                'else',
                f'  touch {d}/build_failed',
                'fi',
            ]
    else:
        for test in tests:
            d = f'"$ROOT"/{q(f"{SCRATCH_DIR}/run/{test.name}")}'
            cmd = settings.run_cmd + (' ' + shlex.join(test.args) if test.args else '')
            for r in (1, 2):
                if r == 2:
                    lines.append(delay)
                lines.append(f'(cd {q(cwd_rel)} && timeout -k 1 {t} sh -c {q(cmd)}'
                    f' < {d}/stdin > {d}/out{r} 2> {d}/err{r}; echo $? > {d}/status{r})')
    return '\n'.join(lines) + '\n'

def _setup_cmds(
    settings: GenTestsSettings,
    driver_build: DriverBuild | None,
    art: TranspileArtifactConfig | None,
) -> list[str]:
    """Commands that build the project once, before any tests run."""
    q = shlex.quote
    if settings.mode == 'driver':
        incs = ' '.join('-I' + q(d) for d in driver_build.include_dirs)
        cflags = ' '.join(q(f) for f in settings.cflags)
        cmds = [f'mkdir -p {q(SCRATCH_DIR)}/obj']
        for i, src in enumerate(driver_build.sources):
            cmds.append(f'{q(settings.cc)} {cflags} {incs} -c {q(src)}'
                f' -o {q(f"{SCRATCH_DIR}/obj/{i}.o")}')
        objs = ' '.join(q(f'{SCRATCH_DIR}/obj/{i}.o') for i in range(len(driver_build.sources)))
        cmds.append(NM_DEFINED.format(objs) + f' > {q(SCRATCH_DIR)}/run/project_syms')
        libs = ' '.join(f'"$({q(settings.cc)} -print-file-name={l})"' for l in ('libc.so.6', 'libm.so.6'))
        cmds.append(NM_DEFINED.format('-D ' + libs) + f' > {q(SCRATCH_DIR)}/run/libc_syms')
        return cmds
    else:
        if art is None or art.build_cmds is None:
            return []
        return list(art.configure_cmds) + list(art.build_cmds)

def observe_tests(
    cfg: Config,
    mvir: MVIR,
    n_c_code: TreeNode,
    tests: list[TestCase],
    settings: GenTestsSettings,
    driver_build: DriverBuild | None,
    art: TranspileArtifactConfig | None,
) -> dict[str, Observation]:
    """
    Build the C code in a sandbox, run each test twice, and collect what it
    printed and its exit code.
    """
    cwd_rel = cfg.relative_path('.')
    inputs = {}
    for test in tests:
        d = f'{SCRATCH_DIR}/run/{test.name}'
        inputs[f'{d}/stdin'] = FileNode.new(mvir, test.stdin).node_id()
        if test.driver is not None:
            inputs[f'{d}/driver.c'] = FileNode.new(mvir, test.driver).node_id()
    if settings.mode == 'driver':
        inputs[f'{SCRATCH_DIR}/{OBS_HEADER_NAME}'] = FileNode.new(mvir, OBS_HEADER).node_id()
    inputs[f'{SCRATCH_DIR}/run_tests.sh'] = FileNode.new(mvir,
        _run_script(tests, settings, driver_build, cwd_rel)).node_id()

    with run_sandbox(cfg, mvir) as sb:
        sb.checkout(n_c_code)
        sb.checkout(TreeNode.new(mvir, files = inputs))

        for cmd in _setup_cmds(settings, driver_build, art):
            setup_cwd = cwd_rel if settings.mode == 'cli' else '.'
            exit_code, logs = sb.run(cmd, shell = True, stream = True, cwd = setup_cwd)
            if exit_code != 0:
                hint = ('; try --exclude-source or --cflags' if settings.mode == 'driver'
                    else '; check the artifact\'s configure_cmds and build_cmds')
                raise CrispError(f'building the C code for testing failed: {cmd}{hint}\n'
                    + _tail(logs))

        sb.run(['sh', f'{SCRATCH_DIR}/run_tests.sh'], stream = True)
        n_results = sb.commit_dir(f'{SCRATCH_DIR}/run',
            path_filter = lambda p: os.path.basename(p) in RESULT_FILES + SYMBOL_FILES)

    def read(name, file):
        node_id = n_results.files.get(f'{SCRATCH_DIR}/run/{name}/{file}')
        return mvir.node(node_id).body() if node_id is not None else None

    def read_syms(path):
        node_id = n_results.files.get(f'{SCRATCH_DIR}/run/{path}')
        if node_id is None:
            return set()
        return set(mvir.node(node_id).body_str().split())

    known_syms = read_syms('libc_syms') | read_syms('project_syms')

    def read_int(name, file):
        b = read(name, file)
        try:
            return int(b.strip()) if b is not None else None
        except ValueError:
            return None

    return {
        test.name: Observation(
            build_failed = read(test.name, 'build_failed') is not None,
            build_log = read(test.name, 'build.log') or b'',
            stdout = (read(test.name, 'out1'), read(test.name, 'out2')),
            stderr = (read(test.name, 'err1'), read(test.name, 'err2')),
            exit_code = (read_int(test.name, 'status1'), read_int(test.name, 'status2')),
            interposed = tuple(sorted((read_syms(f'{test.name}/syms') - {'main'}) & known_syms)),
        )
        for test in tests
    }


# Output

def build_tests_tree(
    mvir: MVIR,
    out_rel: str,
    accepted: list[tuple[TestCase, Observation]],
    rejected: list[tuple[TestCase, Verdict]],
    manifest_extra: dict,
) -> TreeNode:
    files = {}
    def add(path, body):
        files[os.path.normpath(os.path.join(out_rel, path))] = FileNode.new(mvir, body).node_id()

    for test, obs in accepted:
        if test.driver is not None:
            add(f'{test.name}/driver.c', test.driver)
        else:
            add(f'{test.name}/args.json', json.dumps(list(test.args)) + '\n')
        if test.stdin:
            add(f'{test.name}/stdin', test.stdin)
        add(f'{test.name}/expected_stdout', obs.stdout[0])
        add(f'{test.name}/expected_exit_code', f'{obs.exit_code[0]}\n')

    if any(t.driver is not None for t, _ in accepted):
        add(OBS_HEADER_NAME, OBS_HEADER)
    add(RUNNER_NAME, RUNNER_SOURCE)

    manifest = dict(manifest_extra)
    manifest['tests'] = [
        {'name': t.name, 'description': t.description, 'exit_code': obs.exit_code[0]}
        for t, obs in accepted]
    manifest['rejected'] = [
        {'name': t.name, 'description': t.description, 'status': v.status,
            **({'reason': v.reason} if v.status == 'dropped' else {})}
        for t, v in rejected]
    add('manifest.json', json.dumps(manifest, indent = 2) + '\n')
    return TreeNode.new(mvir, files = files)


def _repair_message(failures: list[tuple[TestCase, Verdict]]) -> str:
    parts = []
    for test, verdict in failures:
        if test.driver is not None:
            prev = f'```c\n{test.driver.rstrip()}\n```'
        else:
            prev = f'<args>{json.dumps(list(test.args))}</args>'
            if test.stdin:
                prev += f'\n<stdin>\n{test.stdin}</stdin>'
        parts.append(f'## `{test.name}`: {verdict.status}\n\n```\n{verdict.reason.rstrip()}\n```'
            f'\n\nYour previous test:\n\n{prev}')
    return fill_prompt(prompts.GEN_TESTS_REPAIR, failures = '\n\n'.join(parts))

def _ask(model: str, messages: list[dict], stream: bool = True) -> tuple[dict, dict, str]:
    req = {'messages': list(messages), 'model': model}
    if stream:
        try:
            resp = llm.do_request(req, stream = True)
        except json.JSONDecodeError:
            # `llm.sse_events` passes on the empty events that follow SSE
            # comments (e.g. `: keep-alive` lines sent by some proxies), which
            # aren't valid JSON.  Retry without streaming.
            print('\nwarning: could not parse the streamed LLM response; '
                'retrying without streaming (use --no-stream to skip streaming)')
            resp = llm.do_request(req, stream = False)
    else:
        resp = llm.do_request(req, stream = False)
    try:
        content = resp['choices'][0]['message']['content']
    except (IndexError, KeyError):
        raise CrispError('missing content in LLM output')
    if content is None:
        raise CrispError('LLM output content is `None`')
    return req, resp, content


@dataclass(frozen = True)
class GenTestsResult:
    tests: TreeNode
    record: TreeNode
    num_accepted: int
    num_rejected: int

def generate_tests(
    cfg: Config,
    mvir: MVIR,
    n_c_code: TreeNode,
    specs: list[FragmentSpec],
    settings: GenTestsSettings,
    out_dir: str,
    art: TranspileArtifactConfig | None = None,
    model: str | None = None,
) -> GenTestsResult:
    if settings.mode not in ('driver', 'cli'):
        raise CrispError(f'unknown mode {settings.mode!r}: expected "driver" or "cli"')
    if settings.mode == 'cli' and not settings.run_cmd:
        raise CrispError('cli mode requires --run-cmd')

    cwd_rel = cfg.relative_path('.')
    out_rel = cfg.relative_path(out_dir)
    fragments = extract_fragments(mvir, n_c_code, specs, cwd_rel)
    for f in fragments:
        print(f'fragment: {f.describe()}')

    driver_build = None
    if settings.mode == 'driver':
        driver_build = plan_driver_build(mvir, n_c_code, settings, art)
        if not driver_build.sources:
            raise CrispError('no C sources left to link test drivers against')

    prompt = build_prompt(mvir, n_c_code, fragments, settings, driver_build,
        project = cfg.project_name)
    focus = focus_functions(fragments) if settings.mode == 'driver' else []

    model = model or llm.API_MODEL or cfg.models.rewriter or llm.get_default_model()
    print(f'using model {model!r}')

    messages = [{'role': 'user', 'content': prompt}]
    requests, responses, rounds = [], [], []
    accepted: dict[str, tuple[TestCase, Observation]] = {}
    failed: dict[str, tuple[TestCase, Verdict]] = {}
    dropped: dict[str, tuple[TestCase, Verdict]] = {}

    for round_idx in range(1 + settings.repair_rounds):
        if round_idx > 0:
            if not failed:
                break
            messages.append({'role': 'user', 'content': _repair_message(list(failed.values()))})

        for attempt in range(1 + settings.empty_retries):
            req, resp, content = _ask(model, messages, settings.stream)
            requests.append(req)
            responses.append(resp)
            candidates, warnings = parse_tests(content, settings.mode)
            drops = parse_drops(content) if round_idx > 0 else {}
            if candidates or drops or attempt == settings.empty_retries:
                break
            print(f'warning: round {round_idx}: LLM reply contained no tests '
                f'(attempt {attempt + 1}); asking again')
        messages.append({'role': 'assistant', 'content': content})
        results = {}

        for w in warnings:
            print(f'warning: {w}')
        if round_idx == 0:
            candidates = dedupe_names(candidates)
        else:
            # Only accept replacements for tests that failed.
            unknown = [t.name for t in candidates if t.name not in failed]
            for name in unknown:
                print(f'warning: ignoring repaired test {name!r}, which was not a failed test')
            # Tests the LLM gave up on are rejected for good, rather than being
            # rewritten into a different scenario under the same name.
            for name, reason in drops.items():
                if name not in failed:
                    print(f'warning: ignoring drop of {name!r}, which was not a failed test')
                    continue
                test, _ = failed.pop(name)
                dropped[name] = (test, Verdict('dropped', reason))
                results[name] = asdict(dropped[name][1])
                print(f'round {round_idx}: {name}: dropped ({reason})')
            candidates = [t for t in candidates if t.name not in dropped]
            candidates = list({t.name: t for t in candidates if t.name in failed}.values())

        # Static checks reject tests before they are built.
        to_run = []
        for test in candidates:
            verdict = static_check(test, focus)
            if verdict is None:
                to_run.append(test)
                continue
            results[test.name] = asdict(verdict)
            print(f'round {round_idx}: {test.name}: {verdict.status}')
            failed[test.name] = (test, verdict)
        candidates = to_run

        if not candidates:
            print(f'round {round_idx}: no usable tests in LLM output')
            rounds.append({'round': round_idx, 'warnings': warnings, 'results': results})
            continue

        observations = observe_tests(cfg, mvir, n_c_code, candidates,
            settings, driver_build, art)
        for test in candidates:
            obs = observations[test.name]
            verdict = classify(obs, settings.timeout, settings.max_output_bytes,
                settings.mode, settings.min_records, settings.max_records)
            results[test.name] = asdict(verdict)
            print(f'round {round_idx}: {test.name}: {verdict.status}')
            if verdict.ok:
                accepted[test.name] = (test, obs)
                failed.pop(test.name, None)
            else:
                failed[test.name] = (test, verdict)
        rounds.append({'round': round_idx, 'warnings': warnings, 'results': results})

    accepted_list = list(accepted.values())
    rejected_list = list(failed.values()) + list(dropped.values())

    manifest_extra = {
        'mode': settings.mode,
        'c_code': str(n_c_code.node_id()),
        'fragments': [f.describe() for f in fragments],
        # Paths below are relative to the project root (the CRISP base dir),
        # which is `root_from_tests` relative to the tests directory.
        'root_from_tests': os.path.relpath('.', out_rel),
        'project_dir': cwd_rel,
        'rust_dir': cfg.relative_path(cfg.transpile.output_dir),
        'artifact': art.name if art is not None else None,
        'timeout': settings.timeout,
    }
    if settings.mode == 'driver':
        manifest_extra['build'] = {
            'cc': settings.cc,
            'cflags': list(settings.cflags),
            'force_include': OBS_HEADER_NAME,
            'ldflags': list(settings.ldflags) + [f'-l{l}' for l in driver_build.libs],
            'include_dirs': list(driver_build.include_dirs),
            'sources': list(driver_build.sources),
        }
    else:
        manifest_extra['run_cmd'] = settings.run_cmd
    n_tests = build_tests_tree(mvir, out_rel, accepted_list, rejected_list, manifest_extra)

    def json_file(x):
        return FileNode.new(mvir, json.dumps(x, indent = 2)).node_id()
    n_record = TreeNode.new(mvir, files = {
        'prompt.md': FileNode.new(mvir, prompt).node_id(),
        'fragments.json': json_file([asdict(f) for f in fragments]),
        'settings.json': json_file(asdict(settings)),
        'requests.json': json_file(requests),
        'responses.json': json_file(responses),
        'rounds.json': json_file(rounds),
        'result.json': json_file({
            'c_code': str(n_c_code.node_id()),
            'tests': str(n_tests.node_id()),
            'model': model,
        }),
    })

    mvir.set_tag(TESTS_TAG, n_tests.node_id(), 'gen_tests')
    mvir.set_tag(RECORD_TAG, n_record.node_id(), 'gen_tests')
    return GenTestsResult(n_tests, n_record, len(accepted_list), len(rejected_list))


# Command-line interface

def parse_args(argv = None):
    ap = argparse.ArgumentParser(
        prog = 'python -m crisp.gen_tests',
        description = 'Generate test cases for parts of the C code with an LLM.  '
            'Expected outputs are recorded by running the tests against the original C code.',
        epilog = 'FRAGMENT is FILE, FILE:START-END, FILE:LINE, FILE:FUNC, or FUNC.',
    )
    ap.add_argument('fragments', nargs = '+', metavar = 'FRAGMENT')
    ap.add_argument('--config', '-c', dest = 'config_path', default = 'crisp.toml')
    ap.add_argument('--mvir-storage-dir')
    ap.add_argument('--c-code', default = 'c_code',
        help = 'tag or node ID of the C code tree (default: %(default)s)')
    ap.add_argument('--artifact',
        help = 'name of the `transpile.artifacts` entry to use (default: the only one)')
    ap.add_argument('--mode', choices = ('driver', 'cli'),
        help = '`driver`: the LLM writes C drivers that call project functions; '
            '`cli`: the LLM picks arguments and stdin for the project executable '
            '(default: cli if the artifact sets bin_main, else driver)')
    ap.add_argument('--run-cmd',
        help = 'cli mode: command that runs the built program, relative to the '
            'current directory (e.g. `./build/driver`)')
    ap.add_argument('--num-tests', '-n', type = int, default = 10)
    ap.add_argument('--output-dir', default = 'crisp_tests',
        help = 'where the tests go in the output tree (default: %(default)s)')
    ap.add_argument('--model', help = 'LLM to use (default: $CRISP_API_MODEL or models.rewriter)')
    ap.add_argument('--instructions', help = 'extra instructions to add to the prompt')
    ap.add_argument('--repair-rounds', type = int, default = 3,
        help = 'how many times to ask the LLM to fix rejected tests (default: %(default)s)')
    ap.add_argument('--empty-retries', type = int, default = 2,
        help = 'how many times to re-ask when the LLM reply contains no tests '
            '(default: %(default)s)')
    ap.add_argument('--min-records', type = int, default = 1,
        help = 'driver mode: reject tests that record fewer values (default: %(default)s; '
            'the mutation testing baseline uses 20)')
    ap.add_argument('--max-records', type = int, default = 600,
        help = 'driver mode: reject tests that record more values (default: %(default)s)')
    ap.add_argument('--rerun-delay', type = float, default = 1.1,
        help = 'seconds between the two runs of each test (default: %(default)s)')
    ap.add_argument('--timeout', type = int, default = 10,
        help = 'per-run time limit in seconds (default: %(default)s)')
    ap.add_argument('--max-output-bytes', type = int, default = 65536)
    ap.add_argument('--cc', default = 'cc')
    ap.add_argument('--cflags', default = '-std=gnu11 -O0 -g -w',
        help = 'driver mode: compiler flags (default: %(default)s)')
    ap.add_argument('--ldflags', default = '-lm',
        help = 'driver mode: linker flags (default: %(default)s)')
    ap.add_argument('--exclude-source', action = 'append', default = [], metavar = 'GLOB',
        help = "driver mode: don't link C sources matching GLOB into test drivers")
    ap.add_argument('--no-stream', action = 'store_false', dest = 'stream',
        help = "don't stream the LLM response")
    ap.add_argument('--no-context-headers', action = 'store_false', dest = 'context_headers',
        help = "don't add headers included by the fragments to the prompt")
    return ap.parse_args(argv)

def main(argv = None):
    # Imported here to avoid a circular import; `__main__` has the node ID
    # parsing helpers.
    from .__main__ import parse_node_id_arg

    args = parse_args(argv)
    cfg_kwargs = {}
    if args.mvir_storage_dir is not None:
        cfg_kwargs['mvir_storage_dir'] = os.path.abspath(args.mvir_storage_dir)
    cfg = Config.from_toml_file(args.config_path, **cfg_kwargs)
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    n_c_code = mvir.node(parse_node_id_arg(mvir, args.c_code))
    if not isinstance(n_c_code, TreeNode):
        raise CrispError(f'{args.c_code} is not a tree')

    art = cfg.transpile.artifact(args.artifact) if cfg.transpile.artifacts else None
    mode = args.mode or ('cli' if art is not None and art.bin_main is not None else 'driver')

    settings = GenTestsSettings(
        mode = mode,
        num_tests = args.num_tests,
        run_cmd = args.run_cmd,
        cc = args.cc,
        cflags = tuple(shlex.split(args.cflags)),
        ldflags = tuple(shlex.split(args.ldflags)),
        exclude_sources = tuple(args.exclude_source),
        timeout = args.timeout,
        max_output_bytes = args.max_output_bytes,
        repair_rounds = args.repair_rounds,
        empty_retries = args.empty_retries,
        min_records = args.min_records,
        max_records = args.max_records,
        rerun_delay = args.rerun_delay,
        context_headers = args.context_headers,
        stream = args.stream,
        extra_instructions = args.instructions,
    )
    out_dir = os.path.join(os.path.dirname(os.path.abspath(cfg.config_path)), args.output_dir)

    specs = [parse_fragment_spec(s) for s in args.fragments]
    result = generate_tests(cfg, mvir, n_c_code, specs, settings, out_dir,
        art = art, model = args.model)

    print()
    print(f'accepted {result.num_accepted} tests, rejected {result.num_rejected}')
    print(f'tests = {result.tests.node_id()} (tag {TESTS_TAG!r})')
    print(f'record = {result.record.node_id()} (tag {RECORD_TAG!r})')
    print(f'run `crisp checkout {TESTS_TAG}` to write the tests to disk')

if __name__ == '__main__':
    try:
        main()
    except CrispError as e:
        print(f'error: {e}', file = sys.stderr)
        sys.exit(1)
