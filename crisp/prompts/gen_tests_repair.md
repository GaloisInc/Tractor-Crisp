Some of your tests were rejected.

<<FAILURES>>

Fix each one and return it again, with the same name and in the same format as before.
Return only the corrected tests.  Fix the cause rather than dropping the scenario: do not
shrink the workload or stop recording values to make the problem go away.

If a test cannot be fixed without changing what it checks (for example, it targets a path
that ends in `abort()`, `exit()` or a crash, which cannot be tested), drop it instead of
rewriting it into a different test under the same name:

<drop name="test_name">One sentence explaining why it cannot be fixed.</drop>

Never keep a test's name for a different scenario, and never pad a test with filler
records.
