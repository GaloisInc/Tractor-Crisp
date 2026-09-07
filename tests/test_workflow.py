import json
import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from crisp.mvir import CargoCheckJsonAnalysisNode, FileNode, MVIR, TreeNode

from crisp.workflow import (
    AGENT_FFI_REJECTED_PROMPT, AGENT_SAFETY_PROMPT,
    AGENT_TOLERATED_REVIEW_PROMPT,
    CHECKER_RULES, FFI_ENTRY_POINT_RULES, FFI_SEEN_FINDINGS_CAP,
    TOLERATED_UNSAFETY_RULES, merge_ffi_finding_titles,
    extract_checker_warnings,
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
        for model in (None, 'rescue-model'):
            with self.subTest(model=model), \
                    patch('crisp.workflow.agent.run_rewrite') as rewrite:
                Workflow.agent_safety.__wrapped__(workflow, code, tests, plans,
                    baseline_json=baseline, menu_text='Pinned menu', model=model)
                self.assertEqual(rewrite.call_args.args[3], model or 'test-model')
                self.assertIs(rewrite.call_args.kwargs['unsafe_json'], baseline)
                self.assertEqual(rewrite.call_args.kwargs['extra_code'], {'tests': tests})
                self.assertIs(rewrite.call_args.kwargs['planning_files'], plans)
        workflow.find_unsafe2_json.assert_not_called()


class SafetyStepTest(unittest.TestCase):
    def setUp(self):
        self.w = Mock()
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.w.mvir = MVIR(temp, temp)
        messages = [
            {'reason': 'compiler-artifact', 'fresh': True},
            {'reason': 'compiler-message', 'message': {
                'rendered': 'error[E0308]: mismatched types\n --> src/lib.rs:7:5\n'}},
            {'reason': 'compiler-message', 'message': {
                'rendered': 'help: convert the argument to the expected type\n'}},
            {'reason': 'build-finished', 'success': False},
        ]
        self.failed_check = CargoCheckJsonAnalysisNode.new(self.w.mvir,
            code=TreeNode.new(self.w.mvir, files={}).node_id(),
            exit_code=101,
            json=FileNode.new(self.w.mvir, json.dumps(messages)).node_id(),
            body='\n'.join(json.dumps(j) for j in messages))
        self.w.fuel = FuelCounter('test', fuel=3)
        self.base = self.tree('baseline')
        self.checkpoint = self.tree('checkpoint')
        self.broken = self.tree('broken')
        self.candidate = self.tree('candidate')
        self.plans, self.c_code, self.baseline_json = object(), object(), object()
        self.w.find_unsafe2_json.return_value = self.baseline_json
        self.w.fn_records.return_value = {'crate::target': {'total_unsafe': 2}}
        self.w.type_records.return_value = {}
        self.w.compare_unsafe2_op.return_value.exit_code = 0
        self.w.test_op.return_value.exit_code = 0
        self.w.do_tolerated_review.return_value = (True, None)
        self.w.do_ffi_review.return_value = (True, None)

    @staticmethod
    def tree(name):
        tree = Mock()
        tree.node_id.return_value = name
        return tree

    def run_step(self, limit=3, model=None):
        return Workflow.do_safety_step_agent.__wrapped__(self.w,
            self.base, self.c_code, self.plans, max_invocations=limit, model=model)

    def test_failed_continuation_retries_last_checkpoint_against_original_baseline(self):
        self.w.agent_safety.side_effect = [
            (self.checkpoint, self.plans, 'TARGET: crate::target\nCONTINUE: finish'),
            (self.broken, self.plans, 'CONTINUE: finish'),
            (self.candidate, self.plans, 'DONE'),
        ]
        self.w.cargo_check_json_op.side_effect = [Mock(passed=True), self.failed_check]

        outcome = self.run_step(model='rescue-model')

        self.assertIs(outcome.code, self.candidate)
        self.assertEqual(outcome.invocations, 3)
        calls = self.w.agent_safety.call_args_list
        self.assertEqual([call.args[0] for call in calls],
            [self.base, self.checkpoint, self.checkpoint])
        for call in calls:
            self.assertEqual(call.kwargs['model'], 'rescue-model')
            self.assertIs(call.kwargs['baseline_json'], self.baseline_json)
            self.assertEqual(call.kwargs['menu_text'], calls[0].kwargs['menu_text'])
        self.assertIn('error[E0308]', calls[2].kwargs['prompt_suffix'])
        self.assertIn(' --> src/lib.rs:7:5\nhelp: convert the argument',
            calls[2].kwargs['prompt_suffix'])
        self.assertNotIn('"reason":', calls[2].kwargs['prompt_suffix'])
        self.assertIn('edits were discarded', calls[2].kwargs['prompt_suffix'])
        self.w.compare_unsafe2_op.assert_called_once_with(self.base, self.candidate)
        self.w.test_op.assert_called_once_with(self.candidate, self.c_code)

    def test_last_failed_continuation_judges_prior_checkpoint(self):
        self.w.agent_safety.side_effect = [
            (self.checkpoint, self.plans, 'CONTINUE: finish'),
            (self.broken, self.plans, 'CONTINUE: finish'),
        ]
        self.w.cargo_check_json_op.side_effect = [Mock(passed=True), self.failed_check]
        self.assertIs(self.run_step(limit=2).code, self.checkpoint)
        self.w.compare_unsafe2_op.assert_called_once_with(self.base, self.checkpoint)

    def test_recovered_checkpoint_must_still_pass_final_checker(self):
        self.w.agent_safety.side_effect = [
            (self.checkpoint, self.plans, 'CONTINUE: finish'),
            (self.broken, self.plans, 'CONTINUE: finish'),
        ]
        self.w.cargo_check_json_op.side_effect = [Mock(passed=True), self.failed_check]
        self.w.compare_unsafe2_op.return_value.exit_code = 1
        self.assertIsNone(self.run_step(limit=2).code)
        self.w.test_op.assert_not_called()

    def test_all_nonbuilding_invocations_reject_without_testing_broken_code(self):
        self.w.agent_safety.return_value = (self.broken, self.plans, 'CONTINUE: finish')
        self.w.cargo_check_json_op.return_value = self.failed_check
        self.assertIsNone(self.run_step().code)
        self.assertEqual(self.w.agent_safety.call_count, 3)
        self.w.compare_unsafe2_op.assert_not_called()
        self.w.test_op.assert_not_called()

    def test_last_run_fuel_unit_is_announced_and_candidate_is_judged(self):
        self.w.fuel.fuel = 1
        self.w.agent_safety.return_value = (self.checkpoint, self.plans, 'CONTINUE: finish')
        self.w.cargo_check_json_op.return_value.passed = True
        outcome = self.run_step()
        self.assertIs(outcome.code, self.checkpoint)
        self.assertEqual(outcome.invocations, 1)
        self.assertEqual(self.w.fuel.fuel, 0)
        self.assertIn('final invocation',
            self.w.agent_safety.call_args.kwargs['prompt_suffix'])
        self.w.compare_unsafe2_op.assert_called_once_with(self.base, self.checkpoint)
        self.w.test_op.assert_called_once_with(self.checkpoint, self.c_code)

    def test_single_invocation_step_is_also_announced_as_final(self):
        self.w.agent_safety.return_value = (self.candidate, self.plans, 'DONE')
        self.assertIs(self.run_step(limit=1).code, self.candidate)
        self.assertIn('final invocation',
            self.w.agent_safety.call_args.kwargs['prompt_suffix'])
        self.assertEqual(self.w.fuel.fuel, 2)

    def test_no_run_fuel_stops_before_starting_an_invocation(self):
        self.w.fuel.fuel = 0
        with self.assertRaises(OutOfFuelError):
            self.run_step()
        self.w.agent_safety.assert_not_called()
        self.w.find_unsafe2_json.assert_not_called()


class ReviewRuleParityTest(unittest.TestCase):
    def test_worker_prompt_contains_canonical_review_rules(self):
        prompt = AGENT_SAFETY_PROMPT.format(
            cargo_dir_path='translated_rust',
            after_refactoring_instruction='run tests',
            target_goal='',
            menu='target menu',
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
