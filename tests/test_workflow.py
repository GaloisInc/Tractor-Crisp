import unittest

from crisp.workflow import (
    AGENT_SAFETY_PROMPT, AGENT_TOLERATED_REVIEW_PROMPT,
    CHECKER_RULES, FFI_ENTRY_POINT_RULES, FFI_SEEN_FINDINGS_CAP,
    TOLERATED_UNSAFETY_RULES, merge_ffi_finding_titles,
    extract_checker_warnings,
)


# Finding lines as rendered by `codex exec review` (from a real zlib run).
REPORT = '''
The diff removes `unsafe` from several exported entry points.

- [P1] Restore `unsafe` on `gz_intmax_ffi` — /root/work/translated_rust/src/gzlib.rs:1425-1425
- [P1] Restore `unsafe` on `zlibVersion_ffi` — /root/work/translated_rust/src/zutil.rs:27-27
- [P2] Wrapper contains validation logic — /root/work/translated_rust/src/gzlib.rs:100-120
'''


class ReviewRuleParityTest(unittest.TestCase):
    def test_worker_prompt_contains_canonical_review_rules(self):
        prompt = AGENT_SAFETY_PROMPT.format(
            cargo_dir_path='translated_rust',
            after_refactoring_instruction='run tests',
            target_goal='',
            checker_rules=CHECKER_RULES,
            tolerated_unsafety_rules=TOLERATED_UNSAFETY_RULES,
            ffi_entry_point_rules=FFI_ENTRY_POINT_RULES,
        )

        self.assertIn(TOLERATED_UNSAFETY_RULES, prompt)
        self.assertIn(FFI_ENTRY_POINT_RULES, prompt)
        self.assertIn('notes in it are guidance, not rules', prompt)

    def test_tolerated_reviewer_uses_the_same_rules(self):
        prompt = AGENT_TOLERATED_REVIEW_PROMPT.format(
            cargo_dir_path='translated_rust',
            warnings='warning: moved unsafe operation',
            tolerated_unsafety_rules=TOLERATED_UNSAFETY_RULES,
            ffi_entry_point_rules=FFI_ENTRY_POINT_RULES,
        )

        self.assertIn(TOLERATED_UNSAFETY_RULES, prompt)
        self.assertIn(FFI_ENTRY_POINT_RULES, prompt)
        self.assertIn('dedicated FFI review', prompt)


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
warning: zlib::src::inflate::inflate_ffi: 4 unsafe operations now inside count-exempt FFI entry point (baseline 2); entry points must stay thin
warning: unused variable: `x`
zlib::src::zutil::helper: int-to-pointer casts increased: 0 -> 1
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.83s
'''


class ExtractCheckerWarningsTest(unittest.TestCase):
    def test_extracts_only_checker_warnings(self):
        self.assertEqual(extract_checker_warnings(CHECK_LOGS), [
            'warning: zlib::src::inflate::inflate: raw pointer derefs increased: 1 -> 2',
            'warning: zlib::src::gzlib::gz_open: `unsafe` qualifier changed: false -> true',
            'warning: zlib::src::inflate::inflate_ffi: 4 unsafe operations now '
                'inside count-exempt FFI entry point (baseline 2); '
                'entry points must stay thin',
        ])

    def test_error_lines_are_not_warnings(self):
        # Hard-error diagnostics (no `warning:` prefix) are not extracted.
        self.assertEqual(
            extract_checker_warnings('f: raw pointer derefs increased: 0 -> 1'), [])
