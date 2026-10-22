Scan the C code base in this workspace and document it under `{docs_dir}/`.
Do not modify, create, or delete anything outside `{docs_dir}/`, and do not
build or run the code.

The C sources in this workspace are: {c_sources}.  Identify the project's
primary sources (what its build turns into the library or program) and cover
them in depth, along with the tests (see below); mention build files,
examples, and vendored or contributed code only briefly.  Only the C code is
in this workspace: build files or scripts may refer to other directories,
such as a C-to-Rust translation, that are kept elsewhere, so don't report
those as missing.

# Audience

The docs must serve two kinds of readers, without the C sources at hand:
- agents reimplementing the project from scratch, who need its externally
  visible behavior, contracts, algorithms, and invariants spelled out
  precisely enough to match it; and
- agents refactoring the output of C2Rust, a mechanical C-to-Rust
  translation of this code, into safe Rust.  C2Rust keeps the C names of
  functions, structs, fields, and globals, but expands macros inline and
  captures only the build configuration it was run with.

Both look things up by symbol name, so always use the exact C identifiers
(functions, structs, fields, globals, macros) and name the file each one
lives in.  For macros, describe what they expand to, so readers can
recognize the expansion in translated code.  Describe the C code as it is;
do not propose Rust designs.

# Layout

- `{docs_dir}/README.md`: the entry point.  Purpose of the project, module
  map (file -> responsibility), the main control and data flow, and links
  to every other doc file.
- `{docs_dir}/TESTING.md`: the test suite (see Content).
- Further files by topic or module, as the code base warrants.

# Content

Include everything needed or important for reimplementing the project:
- high-level design decisions and the reasons for them, where evident;
- core data structures: fields, invariants, and how they relate;
- memory ownership: who allocates and who frees each object, lifetimes,
  aliasing and back-pointers, and custom allocator or memory hooks;
- buffers and strings: pointer-plus-length pairs, NUL termination,
  encodings, and in-place mutation;
- global and static mutable state;
- concurrency and reentrancy: the thread-safety contract the library offers
  callers, any locks, atomics, or thread-local state, which objects or
  globals may be shared across threads, and which callbacks may re-enter
  the library (and what state is live when they do).  If the code is
  single-threaded, say so explicitly;
- callbacks, function-pointer tables, and `void *` user data;
- unions, type punning, and layout or alignment assumptions;
- macro-heavy code and what the macros generate;
- error handling and control flow (return codes, `goto` cleanup,
  `setjmp`/`longjmp`);
- the public API and ABI surface: exported functions and data, and structs
  visible to callers;
- portability: dependencies on libc, the OS, or the compiler; conditional
  compilation (`#ifdef` platform checks, `config.h` and build-time feature
  macros), including which configurations change types, struct layouts,
  or the public ABI (e.g. character width or large-file support);
  assumptions about integer sizes, `char` signedness, endianness, and
  alignment; and platform-specific code paths (e.g. entropy, file I/O,
  locale).  Note which configuration the build in this workspace uses,
  since a translation captures only that one;
- performance: hot paths, algorithmic complexity of the core operations,
  and optimizations a reimplementation must preserve (lookup tables,
  fast paths, inlining via macros, avoided copies);
- memory usage: allocation patterns and frequency, buffer growth and
  shrink strategies, pools or arenas, peak-usage drivers, and any
  configured limits (sizes, depths, counts, amplification protections);
- other measurable behavior a reimplementation should match, such as
  streaming or incremental processing guarantees, latency bounds, and
  tunables exposed to callers;
- testing: how the test suite is organized and run, which areas and
  public APIs it covers, test helpers and fixtures, fuzzers or
  benchmarks, and notable gaps in coverage.

Ground every claim in the code.  Mark anything inferred as such, and quote
only short snippets.  Prefer tables and lists to prose, and keep the whole
set of docs compact.
