import unittest

from crisp.workflow import (
    FFI_SEEN_FINDINGS_CAP, merge_ffi_finding_titles,
    extract_checker_warnings,
    parse_waiver_verdicts, unsafety_vector,
    is_blocked_message,
)


# Finding lines as rendered by `codex exec review` (from a real zlib run).
REPORT = '''
The diff removes `unsafe` from several exported entry points.

- [P1] Restore `unsafe` on `gz_intmax_ffi` — /root/work/translated_rust/src/gzlib.rs:1425-1425
- [P1] Restore `unsafe` on `zlibVersion_ffi` — /root/work/translated_rust/src/zutil.rs:27-27
- [P2] Wrapper contains validation logic — /root/work/translated_rust/src/gzlib.rs:100-120
'''


class MergeFfiFindingTitlesTest(unittest.TestCase):
    def test_extracts_titles_without_locations(self):
        self.assertEqual(merge_ffi_finding_titles([], REPORT), [
            'Restore `unsafe` on `gz_intmax_ffi`',
            'Restore `unsafe` on `zlibVersion_ffi`',
            'Wrapper contains validation logic',
        ])

    def test_merge_deduplicates(self):
        seen = merge_ffi_finding_titles([], REPORT)
        self.assertEqual(merge_ffi_finding_titles(list(seen), REPORT), seen)

    def test_bounded_keeps_most_recent(self):
        report = '\n'.join(
            f'- [P1] finding {i} — src/a.rs:{i}-{i}' for i in range(20))
        seen = merge_ffi_finding_titles([], report)
        self.assertEqual(len(seen), FFI_SEEN_FINDINGS_CAP)
        self.assertEqual(seen[-1], 'finding 19')

    def test_clean_report_adds_nothing(self):
        self.assertEqual(merge_ffi_finding_titles([], 'No violations found.'), [])


# Mixed cargo/rustc/checker output from a check-unsafe2 run.
CHECK_LOGS = '''
warning: unused config key `unstable.sparse-registry` in `/w/.cargo/config.toml`
   Compiling zlib v0.1.0 (/root/work/translated_rust)
warning: function pointer comparisons do not produce meaningful results since their addresses are not guaranteed to be unique
  --> src/deflate.rs:1796:40
warning: zlib::src::inflate::inflate: raw pointer derefs increased: 1 -> 2
warning: zlib::src::gzlib::gz_open: `unsafe` qualifier changed: false -> true
warning: unused variable: `x`
zlib::src::zutil::helper: int-to-pointer casts increased: 0 -> 1
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.83s
'''


class ExtractCheckerWarningsTest(unittest.TestCase):
    def test_extracts_only_checker_warnings(self):
        self.assertEqual(extract_checker_warnings(CHECK_LOGS), [
            'warning: zlib::src::inflate::inflate: raw pointer derefs increased: 1 -> 2',
            'warning: zlib::src::gzlib::gz_open: `unsafe` qualifier changed: false -> true',
        ])

    def test_error_lines_are_not_warnings(self):
        # Hard-error diagnostics (no `warning:` prefix) are not extracted.
        self.assertEqual(
            extract_checker_warnings('f: raw pointer derefs increased: 0 -> 1'), [])


ADJUDICATION_REPORT = '''
I examined both functions and their callers.

zlib::src::inflate::inflate_cb still calls the user-supplied output callback,
a C function pointer stored in the stream; there is no safe way to invoke it.

WAIVE zlib::src::inflate::inflate_cb — C callback fn pointer; sound: pointer checked non-null at bind time
REMOVABLE zlib::src::gzlib::gz_error -- error string can be an owned CString field
WAIVE hallucinated::fn_name — not actually in the inventory
'''


class ParseWaiverVerdictsTest(unittest.TestCase):
    def test_parses_both_dash_styles(self):
        verdicts = parse_waiver_verdicts(ADJUDICATION_REPORT)
        self.assertEqual(verdicts['zlib::src::inflate::inflate_cb'],
            ('WAIVE', 'C callback fn pointer; sound: pointer checked non-null at bind time'))
        self.assertEqual(verdicts['zlib::src::gzlib::gz_error'],
            ('REMOVABLE', 'error string can be an owned CString field'))

    def test_prose_is_ignored(self):
        # Mentions of function names in prose don't parse as verdicts.
        self.assertNotIn('callers', parse_waiver_verdicts(ADJUDICATION_REPORT))
        self.assertEqual(len(parse_waiver_verdicts('No verdicts here.')), 0)

    def test_last_verdict_wins(self):
        report = ('WAIVE f — first\n'
                  'REMOVABLE f — changed my mind\n')
        self.assertEqual(parse_waiver_verdicts(report)['f'],
            ('REMOVABLE', 'changed my mind'))


class IsBlockedMessageTest(unittest.TestCase):
    def test_last_line_verdict(self):
        self.assertTrue(is_blocked_message(
            'I recorded the dead end in SAFETY_PLAN.md.\n\n'
            'BLOCKED: inflateBack callback binding cannot lose its last deref\n'))

    def test_prose_mention_is_not_blocked(self):
        self.assertFalse(is_blocked_message(
            'I did not need to answer BLOCKED: the refactor succeeded.\n'
            'All tests pass.'))

    def test_empty_message(self):
        self.assertFalse(is_blocked_message(''))


class UnsafetyVectorTest(unittest.TestCase):
    def test_missing_fields_default(self):
        # Baselines written before `inline_asm` existed must still compare.
        vec = unsafety_vector({'total_unsafe': 2, 'derefs_raw_ptr': 2})
        self.assertEqual(vec['inline_asm'], 0)
        self.assertEqual(vec['uses_static_mut'], {})

    def test_progress_metrics_are_excluded(self):
        # Progress-metric changes must not invalidate a waiver.
        a = unsafety_vector({'total_unsafe': 1, 'calls_unsafe': 1,
            'sig_contains_raw_ptr': 2})
        b = unsafety_vector({'total_unsafe': 1, 'calls_unsafe': 1,
            'sig_contains_raw_ptr': 5})
        self.assertEqual(a, b)
