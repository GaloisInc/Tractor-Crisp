import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from crisp.mvir import FileNode, MVIR, TreeNode

from crisp.workflow import (
    AGENT_SAFETY_PROMPT, AGENT_SAFETY_REVIEW_PROMPT,
    FFI_ENTRY_POINT_RULES, FFI_SEEN_FINDINGS_CAP,
    SAFETY_REVIEW_RULES, merge_ffi_finding_titles,
    review_passed, Workflow,
)


# Finding lines as rendered by `codex exec review` (from a real zlib run).
REPORT = '''
The diff removes `unsafe` from several exported entry points.

- [P1] Restore `unsafe` on `gz_intmax_ffi` — /root/work/translated_rust/src/gzlib.rs:1425-1425
- [P1] Restore `unsafe` on `zlibVersion_ffi` — /root/work/translated_rust/src/zutil.rs:27-27
- [P2] Wrapper contains validation logic — /root/work/translated_rust/src/gzlib.rs:100-120
'''


class SafetyReviewTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.mvir = MVIR(temp.name, temp.name)
        cfg = SimpleNamespace(
            transpile=SimpleNamespace(output_dir='crate'),
            relative_path=lambda path: path,
            models=SimpleNamespace(agent_review='configured-review-model'),
        )
        self.w = Workflow(cfg, self.mvir)
        self.reference = self.tree('api.h', 'typedef void *(*alloc_fn)(void *context);')
        self.review = self.enterContext(patch('crisp.workflow.agent.run_review',
            return_value=('CRISP_REVIEW: PASS\nChecked contracts and callers.', b'log', True)))
        self.enterContext(patch('builtins.print'))

    def tree(self, path, body):
        return TreeNode.new(self.mvir,
            files={path: FileNode.new(self.mvir, body).node_id()})

    def test_callback_and_neutral_cleanup_changes_receive_review(self):
        for old, new in [
            ('type Alloc = unsafe extern "C" fn(*mut u8);',
             'type Alloc = extern "C" fn(*mut u8);'),
            ('fn cleanup() { let result = release_owner(); debug_assert!(result.is_none()); }',
             'fn cleanup() { debug_assert!(release_owner().is_none()); }'),
        ]:
            with self.subTest(new=new):
                self.review.reset_mock()
                baseline = self.tree('crate/src/lib.rs', old)
                candidate = self.tree('crate/src/lib.rs', new)
                self.assertEqual(self.w.do_safety_review(baseline, candidate,
                    self.reference), (True, None))
                self.review.assert_called_once()
                args, kwargs = self.review.call_args
                self.assertEqual(args[3], 'configured-review-model')
                self.assertEqual(args[4:6], (baseline, candidate))
                self.assertEqual(kwargs['extra_code'], {'c_code': self.reference})
                self.assertEqual(kwargs['effort'], 'xhigh')
                self.assertIn('at their original paths', args[2])
                self.assertIn(SAFETY_REVIEW_RULES, args[2])

    def test_identical_tree_skips_model(self):
        code = self.tree('crate/src/lib.rs', 'fn example() {}')
        self.assertEqual(self.w.do_safety_review(code, code, self.reference),
            (True, None))
        self.review.assert_not_called()

    def test_missing_report_and_missing_command_evidence_reject_and_record_reason(self):
        baseline = self.tree('crate/src/lib.rs', 'fn before() {}')
        candidate = self.tree('crate/src/lib.rs', 'fn after() {}')
        for report, inspected in [('', True), ('No findings.', True),
                ('CRISP_REVIEW: PASS', False), ('CRISP_REVIEW: FAIL\nMissing context.', True)]:
            with self.subTest(report=report, inspected=inspected):
                self.review.return_value = report, b'log', inspected
                passed, reason = self.w.do_safety_review(baseline, candidate,
                    self.reference)
                self.assertFalse(passed)
                self.assertTrue(reason.startswith('CRISP_REVIEW: '))
                entry = next(iter(self.mvir.tag_reflog('op_history')))
                op = self.mvir.node(entry.node_id)
                self.assertEqual(op.verdict, 'FAIL')


class ReviewRuleParityTest(unittest.TestCase):
    def test_worker_prompt_contains_canonical_review_rules(self):
        prompt = AGENT_SAFETY_PROMPT.format(
            cargo_dir_path='translated_rust',
            after_refactoring_instruction='run tests',
            target_goal='',
            safety_review_rules=SAFETY_REVIEW_RULES,
            ffi_entry_point_rules=FFI_ENTRY_POINT_RULES,
        )

        self.assertIn(SAFETY_REVIEW_RULES, prompt)
        self.assertIn(FFI_ENTRY_POINT_RULES, prompt)

    def test_semantic_reviewer_uses_the_same_rules(self):
        prompt = AGENT_SAFETY_REVIEW_PROMPT.format(
            cargo_dir_path='translated_rust',
            reference_instruction='Original C is at its original paths.',
            safety_review_rules=SAFETY_REVIEW_RULES,
            ffi_entry_point_rules=FFI_ENTRY_POINT_RULES,
        )
        self.assertIn(SAFETY_REVIEW_RULES, prompt)
        self.assertIn(FFI_ENTRY_POINT_RULES, prompt)
        self.assertIn('overall_explanation', prompt)
        self.assertIn('CRISP_REVIEW: PASS', prompt)


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


class ReviewVerdictTest(unittest.TestCase):
    def test_completed_explicit_approval_passes(self):
        for verdict in ('CRISP_REVIEW: PASS', 'CRISP_REVIEW: PASS.',
                'CRISP_REVIEW: PASS - no defects'):
            with self.subTest(verdict=verdict):
                self.assertTrue(review_passed(verdict + '\nChecked the affected callers.', True))

    def test_missing_incomplete_ambiguous_and_rejecting_reports_fail(self):
        for report, ran_commands in [
            ('', True),
            ('No findings.', True),
            ('Review was interrupted.', True),
            ('Unable to assess the callback lifetime.', True),
            ('CRISP_REVIEW: FAIL\nMissing API context.', True),
            ('CRISP_REVIEW: PASS', False),
            ('CRISP_REVIEW: PASSING', True),
            ('CRISP_REVIEW: PASS_FAIL', True),
            ('CRISP_REVIEW: PASS\nCRISP_REVIEW: FAIL', True),
            ('CRISP_REVIEW: PASS.\nCRISP_REVIEW: FAIL', True),
            ('CRISP_REVIEW: PASS - no defects', False),
            ('CRISP_REVIEW: PASS.\n  - [P2] Missed cleanup', True),
            ('CRISP_REVIEW: PASS\n' + REPORT, True),
            ('CRISP_REVIEW: PASS\n  - [P2] Missed cleanup', True),
        ]:
            with self.subTest(report=report, ran_commands=ran_commands):
                self.assertFalse(review_passed(report, ran_commands))
