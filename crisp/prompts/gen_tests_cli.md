You are writing **characterisation tests** for a C program.

We have a C program `<<PROJECT>>` that is being translated to Rust.  A characterisation
test pins down what the original does so the translation can be checked against it: it
runs the program on fixed inputs and *records* what the program prints.  Each test is run
on the original program and its standard output and exit status become the expected
output; the same test is then run against the translation and the results are compared.
You choose the inputs only, so never work out or write down expected output yourself.

Write <<NUM_TESTS>> tests.  Each test is one run of the program, from the project
directory, as:

```
<<RUN_CMD>> <args...> < <stdin>
```

## The program

All files in the project:

<<FILE_LIST>>

<<CONTEXT>>

## Where to focus

<<FRAGMENTS>>

Choose inputs that drive execution through this code and the code around it.  A
translation that gets one path right may still be wrong nearby.

## Hard constraints

1. **Command line and stdin only.**  If the program needs an input file, pass the data on
   stdin if the program supports that (e.g. `-` as the file name); otherwise only name
   files that already exist in the project.  Never write files outside the current
   directory.

2. **Workload shape.**  Typical inputs at several sizes, boundary inputs (empty input, a
   single item, the largest sizes and values the program accepts, zero, negative numbers)
   and error paths (missing or malformed arguments, invalid input).  Aim each input at a
   *behavioural edge* of the code under test, where a subtly wrong translation would
   differ, rather than sampling typical inputs:
   - **Sizes** 0, 1, and one either side of every limit or cutoff in the code (buffer
     sizes, line lengths, counts).  Use enough data to cross those cutoffs.
   - **Values** at the extremes the program accepts and one past each point where the
     handling changes.
   - **Every mode**: each option, flag and subcommand that reaches the code under test.
   - **Damaged input**: truncated, corrupted or malformed data.
   - At least one **realistic** input, such as genuine text for a parser.

   Each test covers one scenario and has a short, descriptive, snake_case name.

3. **Record behaviour, never representation.**  Avoid inputs and options that make the
   program print pointers or addresses, the time, random values, or anything that depends
   on the platform, the environment or the absolute path of the project.

4. **No undefined behaviour.**  Don't choose inputs that you can see would make the program
   read out of bounds, overflow a buffer or crash.  Output observed from undefined
   behaviour is not a valid expectation.  **Paths that end in `abort()`, a failed `assert`
   or a crash cannot be tested**, so never aim a test at them: test the inputs just inside
   the limit that triggers them instead.  (A normal `exit()` with an error status is fine.)

5. **Deterministic and fast.**  Each test is run twice and must produce the same output
   and exit status both times.  Each run must finish in well under <<TIMEOUT>> seconds and
   print at most a few kilobytes.

## How tests are checked

Every test is run on the original program before it is accepted.  A test is rejected, and
returned to you with the reason, if the program crashes or exceeds the time limit, prints
more than a few kilobytes, or prints something different or exits differently when run a
second time a moment later.  A non-zero exit status is fine: it is recorded as part of the
expected behaviour.
<<EXTRA_INSTRUCTIONS>>
## Output

Each test in this format, one after another, nothing else:

<test name="short_snake_case_name">
<description>
One sentence describing the scenario.
</description>
<args>["--example-flag", "value"]</args>
<stdin>
Optional: text for standard input.  Omit this element if the test doesn't read stdin.
</stdin>
</test>

`<args>` is a JSON array of strings; use `[]` for no arguments.
