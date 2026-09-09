import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from crisp.mvir import FileNode, MVIR, TreeNode
from crisp.config import ModelsConfig

from crisp.workflow import (
    AGENT_FFI_REJECTED_PROMPT, AGENT_SAFETY_PROMPT,
    AGENT_SAFETY_REVIEW_PROMPT,
    CHECKER_RULES, FFI_ENTRY_POINT_RULES, FFI_SEEN_FINDINGS_CAP,
    SAFETY_REVIEW_RULES, merge_ffi_finding_titles,
    review_passed,
    parse_verdict, parse_target,
    menu_targets, format_menu, Workflow,
    FuelCounter, OutOfFuelError,
)


# Finding lines as rendered by `codex exec review` (from a real zlib run).
REPORT = '''
The diff removes `unsafe` from several exported entry points.

- [P1] Restore `unsafe` on `gz_intmax_ffi` — /root/work/translated_rust/src/gzlib.rs:1425-1425
- [P1] Restore `unsafe` on `zlibVersion_ffi` — /root/work/translated_rust/src/zutil.rs:27-27
- [P2] Wrapper contains validation logic — /root/work/translated_rust/src/gzlib.rs:100-120
'''


class SafetyWorkerTest(unittest.TestCase):
    def test_worker_receives_baseline_menu_and_feedback(self):
        cfg = SimpleNamespace(
            transpile=SimpleNamespace(output_dir='crate'),
            relative_path=lambda path: path,
            test_command=None,
            models=ModelsConfig(),
        )
        temp = self.enterContext(tempfile.TemporaryDirectory())
        workflow = Workflow(cfg, MVIR(temp, temp))
        baseline = object()
        workflow.find_unsafe2_json = Mock(return_value=baseline)
        workflow.fn_records = Mock(return_value={
            'crate::first': {'total_unsafe': 2},
            'crate::second': {'total_unsafe': 1}})
        workflow.type_records = Mock(return_value={})
        code, plans, tests = object(), object(), object()
        with patch('crisp.workflow.agent.run_rewrite') as rewrite:
            Workflow.agent_safety.__wrapped__(workflow, code, tests, plans,
                suppressed=frozenset({'crate::second'}),
                review_feedback={'crate::first': 'FIRST full report',
                    'crate::second': 'SECOND full report'})
        self.assertEqual(rewrite.call_args.args[3], ModelsConfig().agent_loop)
        self.assertIs(rewrite.call_args.kwargs['unsafe_json'], baseline)
        workflow.find_unsafe2_json.assert_called_once_with(code)
        extra = rewrite.call_args.kwargs['extra_code']
        self.assertIs(extra['tests'], tests)
        reports = workflow.mvir.node(extra['review_feedback'].files[
            'SAFETY_REVIEW_FEEDBACK.md']).body_str()
        self.assertIn('## crate::first', reports)
        self.assertIn('FIRST full report', reports)
        self.assertIn('## crate::second', reports)
        self.assertIn('SECOND full report', reports)
        prompt = rewrite.call_args.args[2]
        self.assertIn('- crate::first: 2', prompt)
        self.assertIn('SAFETY_REVIEW_FEEDBACK.md', prompt)
        self.assertIn('do not target: `crate::second`', prompt)
        self.assertNotIn('FIRST full report', prompt)
        self.assertIs(rewrite.call_args.kwargs['planning_files'], plans)


class SafetyStepTest(unittest.TestCase):
    def setUp(self):
        self.w = Mock()
        self.w.fuel = FuelCounter('test', fuel=3)
        self.base = self.tree('baseline')
        self.candidate = self.tree('candidate')
        self.plans, self.c_code = object(), object()
        self.w.agent_safety.return_value = (
            self.candidate, self.plans, 'TARGET: crate::target\nDONE')
        self.w.compare_unsafe2_op.return_value.exit_code = 0
        self.w.test_op.return_value.exit_code = 0
        self.w.do_safety_review.return_value = (True, None)

    @staticmethod
    def tree(name):
        tree = Mock()
        tree.node_id.return_value = name
        return tree

    def run_step(self, **kwargs):
        return Workflow.do_safety_step_agent.__wrapped__(self.w,
            self.base, self.c_code, self.plans, **kwargs)

    def test_one_invocation_uses_one_fuel_and_runs_gates(self):
        self.w.fuel.fuel = 1
        feedback = {'crate::target': 'Use initialized storage'}
        outcome = self.run_step(review_feedback=feedback, suppressed=frozenset({'other'}))
        self.assertIs(outcome.code, self.candidate)
        self.assertEqual(self.w.fuel.fuel, 0)
        self.w.agent_safety.assert_called_once()
        call = self.w.agent_safety.call_args
        self.assertEqual(call.kwargs['review_feedback'], feedback)
        self.assertEqual(call.kwargs['suppressed'], frozenset({'other'}))
        self.w.compare_unsafe2_op.assert_called_once_with(self.base, self.candidate)
        self.w.test_op.assert_called_once_with(self.candidate, self.c_code)
        self.w.do_safety_review.assert_called_once_with(self.base, self.candidate,
            self.c_code, self.w.compare_unsafe2_op.return_value)

    def test_blocked_discards_candidate_without_gates(self):
        self.w.agent_safety.return_value = (
            self.candidate, self.plans, 'TARGET: crate::target\nBLOCKED: prerequisite')
        outcome = self.run_step()
        self.assertIs(outcome.code, self.base)
        self.assertEqual(outcome.note, 'prerequisite')
        self.w.compare_unsafe2_op.assert_not_called()
        self.w.test_op.assert_not_called()

    def test_unchanged_candidate_skips_gates(self):
        self.w.agent_safety.return_value = (self.base, self.plans, 'DONE')
        self.assertIs(self.run_step().code, self.base)
        self.w.compare_unsafe2_op.assert_not_called()
        self.w.test_op.assert_not_called()

    def test_failed_checker_rejects_without_tests_or_review(self):
        self.w.compare_unsafe2_op.return_value.exit_code = 1
        self.assertIsNone(self.run_step().code)
        self.w.test_op.assert_not_called()
        self.w.do_safety_review.assert_not_called()

    def test_failed_tests_reject_without_review(self):
        self.w.test_op.return_value.exit_code = 1
        self.assertIsNone(self.run_step().code)
        self.w.do_safety_review.assert_not_called()

    def test_no_run_fuel_stops_before_starting_an_invocation(self):
        self.w.fuel.fuel = 0
        with self.assertRaises(OutOfFuelError):
            self.run_step()
        self.w.agent_safety.assert_not_called()


    def test_done_candidate_runs_one_review_after_mechanical_gates(self):
        self.w.agent_safety.return_value = (self.candidate, self.plans, 'DONE')
        self.assertIs(self.run_step().code, self.candidate)
        gates = [call[0] for call in self.w.method_calls
            if call[0] in ('compare_unsafe2_op', 'test_op', 'do_safety_review')]
        self.assertEqual(gates, ['compare_unsafe2_op', 'test_op', 'do_safety_review'])
        self.w.do_safety_review.assert_called_once_with(self.base, self.candidate,
            self.c_code, self.w.compare_unsafe2_op.return_value)

    def test_failed_mechanical_gate_skips_review(self):
        for failed_gate in ('compare_unsafe2_op', 'test_op'):
            with self.subTest(failed_gate=failed_gate):
                self.w.reset_mock()
                self.w.fuel.fuel = 3
                self.w.compare_unsafe2_op.return_value.exit_code = 0
                self.w.test_op.return_value.exit_code = 0
                getattr(self.w, failed_gate).return_value.exit_code = 1
                self.w.agent_safety.return_value = (self.candidate, self.plans, 'DONE')
                self.assertIsNone(self.run_step().code)
                self.w.do_safety_review.assert_not_called()

    def test_blocked_or_unchanged_step_skips_review(self):
        for code, verdict in ((self.candidate, 'BLOCKED: prerequisite'), (self.base, 'DONE')):
            with self.subTest(verdict=verdict):
                self.w.agent_safety.return_value = (code, self.plans, verdict)
                self.assertIs(self.run_step().code, self.base)
                self.w.do_safety_review.assert_not_called()
                self.w.test_op.assert_not_called()

    def test_review_failure_returns_report_for_existing_feedback_path(self):
        self.w.agent_safety.return_value = (self.candidate, self.plans, 'DONE')
        report = 'CRISP_REVIEW: FAIL\n- [P1] Cleanup disappears in release'
        self.w.do_safety_review.return_value = (False, report)
        outcome = self.run_step()
        self.assertIsNone(outcome.code)
        self.assertEqual(outcome.report, report)

    def test_simulation_reviewer_does_not_receive_hidden_original_tests(self):
        self.w.agent_safety_no_tests.return_value = (self.candidate, self.plans, 'DONE')
        self.w.cargo_check_json_op.return_value.passed = True
        result = Workflow.do_safety_step_agent_sim_no_tests.__wrapped__(self.w,
            self.base, self.c_code, self.plans)
        self.assertEqual(result, (self.candidate, self.plans))
        self.w.do_safety_review.assert_called_once_with(self.base, self.candidate,
            None, self.w.compare_unsafe2_op.return_value)


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
        self.check = Mock()
        self.check.body_str.return_value = 'PASS: no diagnostics'
        self.review = self.enterContext(patch('crisp.workflow.agent.run_review',
            return_value=('CRISP_REVIEW: PASS\nChecked contracts and callers.', b'log', True)))
        self.enterContext(patch('builtins.print'))

    def tree(self, path, body):
        return TreeNode.new(self.mvir,
            files={path: FileNode.new(self.mvir, body).node_id()})

    def test_callback_and_neutral_cleanup_changes_receive_review_without_warnings(self):
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
                    self.reference, self.check), (True, None))
                self.review.assert_called_once()
                args, kwargs = self.review.call_args
                self.assertEqual(args[3], 'configured-review-model')
                self.assertEqual(args[4:6], (baseline, candidate))
                self.assertEqual(kwargs['extra_code'], {'c_code': self.reference})
                self.assertEqual(kwargs['effort'], 'xhigh')
                self.assertIn('at their original paths', args[2])
                self.assertIn('PASS: no diagnostics', args[2])
                self.assertIn(SAFETY_REVIEW_RULES, args[2])

    def test_identical_tree_skips_model(self):
        code = self.tree('crate/src/lib.rs', 'fn example() {}')
        self.assertEqual(self.w.do_safety_review(code, code, self.reference, self.check),
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
                    self.reference, self.check)
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
            menu='target menu',
            checker_rules=CHECKER_RULES,
            safety_review_rules=SAFETY_REVIEW_RULES,
            ffi_entry_point_rules=FFI_ENTRY_POINT_RULES,
        )

        self.assertIn(SAFETY_REVIEW_RULES, prompt)
        self.assertIn(FFI_ENTRY_POINT_RULES, prompt)
        self.assertIn('notes in it are guidance, not rules', prompt)

    def test_semantic_reviewer_uses_the_same_rules(self):
        prompt = AGENT_SAFETY_REVIEW_PROMPT.format(
            cargo_dir_path='translated_rust',
            reference_instruction='Original C is at its original paths.',
            checker_diagnostics='PASS',
            safety_review_rules=SAFETY_REVIEW_RULES,
            ffi_entry_point_rules=FFI_ENTRY_POINT_RULES,
        )
        self.assertIn(SAFETY_REVIEW_RULES, prompt)
        self.assertIn(FFI_ENTRY_POINT_RULES, prompt)
        self.assertIn('overall_explanation', prompt)
        self.assertIn('CRISP_REVIEW: PASS', prompt)


class RejectedReviewPromptTest(unittest.TestCase):
    def test_report_names_the_target_it_describes(self):
        prompt = AGENT_FFI_REJECTED_PROMPT.format(
            target='zlib::src::inffast::inflate_fast',
            report='Keep the exported wrapper thin.',
        )

        self.assertIn('attempt at `zlib::src::inffast::inflate_fast`', prompt)
        self.assertIn('rejection of that unrelated work', prompt)
        self.assertNotIn('attempt at this step', prompt)


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


class ParseVerdictTest(unittest.TestCase):
    def test_blocked_with_note(self):
        self.assertEqual(parse_verdict(
            'Updated the plan.\n\nBLOCKED: gz_read, gz_look — E0277'),
            ('blocked', 'gz_read, gz_look — E0277'))

    def test_done_explicit_and_default(self):
        self.assertEqual(parse_verdict('All finished.\nDONE'), ('done', ''))
        self.assertEqual(parse_verdict('No verdict line here.'), ('done', ''))
        self.assertEqual(parse_verdict(''), ('done', ''))

    def test_prose_mention_is_not_a_verdict(self):
        self.assertEqual(parse_verdict(
            'The work fit one invocation.\nAll done.'),
            ('done', ''))


class ParseTargetTest(unittest.TestCase):
    def test_first_declaration_wins(self):
        msg = 'TARGET: zlib::src::deflate::deflate\nwork...\nTARGET: other'
        self.assertEqual(parse_target(msg), 'zlib::src::deflate::deflate')

    def test_field_target_and_absence(self):
        self.assertEqual(parse_target('TARGET: gz_state.path\n...'),
            'gz_state.path')
        self.assertIsNone(parse_target('no declaration'))


# Trimmed inventory records in the shape of `unsafe_json/<crate>.json`.
MENU_FNS = {
    'zlib::src::inflate::inflate': {
        'filename': 'src/inflate.rs', 'total_unsafe': 503},
    'zlib::src::deflate::deflate': {
        'filename': 'src/deflate.rs', 'total_unsafe': 186},
    'zlib::src::gzlib::gzbuffer_ffi': {
        'filename': 'src/gzlib.rs', 'total_unsafe': 4,
        'ffi_symbol': 'gzbuffer'},
    'zlib::src::adler32::adler32_impl': {
        'filename': 'src/adler32.rs', 'total_unsafe': 0},
}
MENU_TYPES = {
    'zlib::src::gzlib::gz_state': {
        'filename': 'src/gzlib.rs',
        'field_contains_raw_ptr': {'path': 1, 'next': 1, 'want': 0}},
    'zlib::zlib_h::z_stream_s': {
        'filename': 'src/zlib_h.rs',
        'field_contains_raw_ptr': {'state': 5, 'zalloc': 2}},
    'zlib::src::zutil::alloc_func': {
        'filename': 'src/zutil.rs',
        'field_contains_raw_ptr': {'type': 1}},
}


class MenuTargetsTest(unittest.TestCase):
    def test_functions_by_mass_and_fields(self):
        fns, fields = menu_targets(MENU_FNS, MENU_TYPES)
        self.assertEqual(fns, [
            ('zlib::src::inflate::inflate', 503),
            ('zlib::src::deflate::deflate', 186),
        ])
        self.assertEqual(fields, [
            ('zlib::src::gzlib::gz_state.next', 1),
            ('zlib::src::gzlib::gz_state.path', 1),
        ])

    def test_abi_header_fields_excluded(self):
        _, fields = menu_targets({}, MENU_TYPES)
        self.assertNotIn('zlib::zlib_h::z_stream_s.state',
            [name for name, _ in fields])

    def test_alias_pseudo_field_excluded(self):
        _, fields = menu_targets({}, MENU_TYPES)
        self.assertNotIn('zlib::src::zutil::alloc_func.type',
            [name for name, _ in fields])

    def test_suppression(self):
        fns, fields = menu_targets(MENU_FNS, MENU_TYPES, {
            'zlib::src::inflate::inflate', 'zlib::src::gzlib::gz_state.path'})
        self.assertEqual([n for n, _ in fns],
            ['zlib::src::deflate::deflate'])
        self.assertEqual([n for n, _ in fields],
            ['zlib::src::gzlib::gz_state.next'])


class FormatMenuTest(unittest.TestCase):
    def test_sections_render(self):
        text = format_menu(MENU_FNS, MENU_TYPES)
        self.assertIn('- src/inflate.rs: 503', text)
        self.assertIn('- zlib::src::inflate::inflate: 503', text)
        self.assertIn('`Type.field`', text)
        self.assertNotIn('gzbuffer_ffi', text)
        self.assertNotIn('adler32_impl', text)

    def test_no_field_section_when_empty(self):
        text = format_menu(MENU_FNS, {})
        self.assertNotIn('raw pointers', text)

    def test_suppressed_targets_named(self):
        text = format_menu(MENU_FNS, MENU_TYPES,
            {'zlib::src::inflate::inflate'})
        self.assertIn('do not target: `zlib::src::inflate::inflate`', text)
        self.assertNotIn('- zlib::src::inflate::inflate: 503', text)
        self.assertNotIn('Deferred', format_menu(MENU_FNS, MENU_TYPES))

    def test_stale_suppressed_targets_are_not_rendered(self):
        text = format_menu(MENU_FNS, MENU_TYPES, {'no::longer::live'})
        self.assertNotIn('no::longer::live', text)
        self.assertNotIn('Deferred', text)

    def test_function_list_is_bounded(self):
        fns = {f'c::f{i:03}': {'filename': 'src/a.rs', 'total_unsafe': i + 1}
            for i in range(30)}
        text = format_menu(fns, {})
        self.assertIn('- c::f029: 30', text)
        self.assertNotIn('- c::f005: 6', text)
        # The full mass still shows through the per-file total.
        self.assertIn('- src/a.rs: 465', text)
