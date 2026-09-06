import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from crisp.workflow import (
    AGENT_SAFETY_PROMPT, AGENT_TOLERATED_REVIEW_PROMPT,
    CHECKER_RULES, FFI_ENTRY_POINT_RULES, FFI_SEEN_FINDINGS_CAP,
    TOLERATED_UNSAFETY_RULES, merge_ffi_finding_titles,
    extract_checker_warnings,
    parse_verdict, parse_target, Workflow,
)


# Finding lines as rendered by `codex exec review` (from a real zlib run).
REPORT = '''
The diff removes `unsafe` from several exported entry points.

- [P1] Restore `unsafe` on `gz_intmax_ffi` — /root/work/translated_rust/src/gzlib.rs:1425-1425
- [P1] Restore `unsafe` on `zlibVersion_ffi` — /root/work/translated_rust/src/zutil.rs:27-27
- [P2] Wrapper contains validation logic — /root/work/translated_rust/src/gzlib.rs:100-120
'''


class SafetyBaselineTest(unittest.TestCase):
    def test_continuation_passes_starting_baseline_to_refactored_agent(self):
        cfg = SimpleNamespace(
            transpile=SimpleNamespace(output_dir='crate'),
            relative_path=lambda path: path,
            test_command=None,
            models=SimpleNamespace(agent_loop='test-model'),
        )
        workflow = Workflow(cfg, object())
        workflow.find_unsafe2_json = Mock()
        baseline = object()
        code, plans, tests = object(), object(), object()
        with patch('crisp.workflow.agent.run_rewrite') as rewrite:
            Workflow.agent_safety.__wrapped__(workflow, code, tests, plans,
                baseline_json=baseline)
        self.assertIs(rewrite.call_args.kwargs['unsafe_json'], baseline)
        self.assertEqual(rewrite.call_args.kwargs['extra_code'], {'tests': tests})
        self.assertIs(rewrite.call_args.kwargs['planning_files'], plans)
        workflow.find_unsafe2_json.assert_not_called()


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


class ParseVerdictTest(unittest.TestCase):
    def test_blocked_with_note(self):
        self.assertEqual(parse_verdict(
            'Updated the plan.\n\nBLOCKED: gz_read, gz_look — E0277'),
            ('blocked', 'gz_read, gz_look — E0277'))

    def test_continue_with_handoff(self):
        self.assertEqual(parse_verdict(
            'Landed the owner type.\nCONTINUE: migrate the callers next'),
            ('continue', 'migrate the callers next'))

    def test_done_explicit_and_default(self):
        self.assertEqual(parse_verdict('All finished.\nDONE'), ('done', ''))
        self.assertEqual(parse_verdict('No verdict line here.'), ('done', ''))
        self.assertEqual(parse_verdict(''), ('done', ''))

    def test_prose_mention_is_not_a_verdict(self):
        self.assertEqual(parse_verdict(
            'I did not need CONTINUE: the work fit one invocation.\nAll done.'),
            ('done', ''))


class ParseTargetTest(unittest.TestCase):
    def test_first_declaration_wins(self):
        msg = 'TARGET: zlib::src::deflate::deflate\nwork...\nTARGET: other'
        self.assertEqual(parse_target(msg), 'zlib::src::deflate::deflate')

    def test_field_target_and_absence(self):
        self.assertEqual(parse_target('TARGET: gz_state.path\n...'),
            'gz_state.path')
        self.assertIsNone(parse_target('no declaration'))
