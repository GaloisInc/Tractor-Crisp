You are writing **C characterisation tests** for a library.

We have a C library `<<PROJECT>>` that is being translated to Rust.  A characterisation test pins down what the original does so the translation can be checked against it: it runs a fixed workload through the library's API and *records* every result with the recording functions below, instead of asserting.  Each test is run on the original C library and the values it records become its expected output; the same test is then run against the translation and the recorded values are compared.  So never work out or write down expected values yourself.

Write <<NUM_TESTS>> tests.  Each test is one C file with a `main()` that runs on the original library and exits 0.

## The library

All files in the project:

<<FILE_LIST>>

<<CONTEXT>>

## Where to focus

<<FRAGMENTS>>

Shape the tests around this code: drive it, and the operations around it, through the library's API.  A translation that gets one call right may still be wrong nearby.

## Hard constraints

1. **Library API only.**  Call the library the way a normal caller would.  Any non-`static` function or global variable of the library may be used; `static` functions may not, so reach them through the functions that call them.  No copied library code, no `#include` of a `.c` file.

2. **Record with the three recording functions**, which are already defined for you (do not declare or define them, and do not include anything to get them):

   ```c
   void obs_ll (const char *label, long long v);    /* integers, booleans, counts, status */
   void obs_dbl(const char *label, double v);       /* floating point */
   void obs_str(const char *label, const char *s);  /* NUL-terminated strings (NULL is fine) */
   ```

   - **Labels are unique, printable ASCII without spaces, and hierarchical**:
     `scenario/step/what`, e.g. `insert_100/len`, `lookup/absent_key_7/found`,
     `encode/int64/neg_min/byte3`.  In a loop, put the index in the label.  Two records
     in one test may never share a label.
   - **Record every result the workload produces**: return values, status codes,
     out-parameters, and the contents of output buffers and structs, element by element or
     field by field.  Prefer many small facts over a few large ones; each is compared
     independently.
   - **Record behaviour, never representation.**  Do NOT record pointers or addresses,
     allocated sizes or capacities, memory footprints, hash values of pointers, iteration
     order of hash-based containers (unless the API defines the order), uninitialised
     bytes, or anything that depends on the platform, the allocator or word size.  Opaque
     handles are recorded as `!= NULL` flags only.  Also do not record bytes of a buffer that the function did not write (e.g. past the terminator), or the contents of output buffers after a call that reported an error.
   - **Record properties, not one witness.**  Record each element of an output, not just its length or first byte; record the intermediate results of a sequence of calls, not only the final one.
   - **Record order-dependent results as a sequence**, element by element in the order the API returns them.
   - **Print nothing else to stdout.**  Every line of stdout must come from a recording
     function.  You may print diagnostics to stderr.

3. **Workload shape.**  Several scales (a handful of elements, then tens, then a few hundred if the library is fast enough) plus boundary inputs (empty, single element, largest representable, zero, negative, exact powers of two where sizes matter) and error-path calls.  Realistic sequences of operations, not one call per function.  Aim each input at a *behavioural edge* of the code under test, where a subtly wrong translation would differ, rather than sampling typical inputs:
   - **Sizes** 0, 1, exact fit (an output buffer exactly as large as required, not one with slack), and one either side of every limit or cutoff in the code (capacities, resize thresholds, small/large code paths).  Use enough elements to cross those cutoffs.
   - **Values** at the extremes of their type and one past each point where the handling changes.
   - **NULL pointers** wherever the code accepts them without dereferencing: with a length of 0, or where it checks for NULL.  Translations often mishandle these.
   - **Every mode**: each enum variant, flag and configuration the API accepts.
   - **Round trips** for inverse pairs (encode/decode, push/pop, serialise/parse): record both the intermediate and the final result.
   - **Sequences**: states reachable only through a particular order of calls (wraparound, refill after empty, reuse after reset), and the *second* call on the same object.
   - **Damaged input** where the code validates it: truncated, corrupted (e.g. a bad checksum) or malformed data.
   - At least one **realistic** input, such as genuine text for a parser.

   Each test covers one scenario and has a short, descriptive, snake_case name.

4. **No undefined behaviour.**  No out-of-bounds accesses, use after free, signed
   overflow, or NULL arguments the code would dereference.  A value recorded from
   undefined behaviour is not a valid expectation.  NULL is fine where the code never
   dereferences it, e.g. a NULL pointer with a length of 0, or a NULL argument the code
   checks for.

5. **Deterministic, and driven only through the library's API.**  No randomness (a fixed LCG in the test is fine), no time, no network, no reading files you did not create. Each test is run twice and must record the same values both times.  In particular:
   - **Never define, wrap or replace a function the library or libc provides** (e.g. your own `malloc`, `rand`, `clock_gettime`) to control or count what the library does.
   - **Never capture or redirect stdout/stderr** (`dup2`, `freopen`, pipes).
   - If the library itself reads the clock or a random source, record only what holds for every possible time/random value (e.g. lengths, relations between two calls you make).

6. **Exit 0, and be fast.**  `return 0;` from `main`, and finish in well under <<TIMEOUT>> seconds.  You may use `assert` for your own setup sanity, but a test that aborts recorded nothing useful.  **Paths that end in `abort()`, `exit()`, a failed `assert` or a crash cannot be tested**, so never aim a test at them: test the inputs just inside the limit that triggers them instead.

7. **Self-contained.**  One C file per test, including the library's headers by the path shown above (e.g. `#include "<<HEADER_EXAMPLE>>"`).  Each test is built roughly like this:

   ```
   <<BUILD_CMD>>
   ```

## How tests are checked

Every test is built and run on the original library before it is accepted.  A test is rejected, and returned to you with the reason, if it:

- `#include`s a `.c` file, defines `obs_ll`/`obs_dbl`/`obs_str`, or uses `dup`, `dup2`, `freopen`, `setvbuf`, `setbuf` or `STDOUT_FILENO`;
- never calls the code under test;
- defines a non-`static` function or variable that libc or the library already provides (make all your helpers `static`);
- fails to build, crashes, exceeds the time limit, or exits non-zero;
- records nothing, records fewer than <<MIN_RECORDS>> or more than <<MAX_RECORDS>> values, or records the same label twice;
- records different values or exits differently when run a second time a moment later.
<<EXTRA_INSTRUCTIONS>>
## Output

Each test in this format, one after another, nothing else:

<test name="short_snake_case_name">
<description>
One sentence describing the scenario.
</description>
<driver>
```c
#include "<<HEADER_EXAMPLE>>"
...
int main(void) { ...; return 0; }
```
</driver>
</test>
