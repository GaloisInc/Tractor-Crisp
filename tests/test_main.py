import tempfile
import unittest

from crisp.__main__ import (
    prior_review_findings,
    update_review_feedback,
    update_target_deferrals,
)
from crisp.mvir import CodexReviewOpNode, FileNode, MVIR, TreeNode


class TargetDeferralsTest(unittest.TestCase):
    def test_failures_expire_after_an_accepted_reduction(self):
        deferred = set()
        update_target_deferrals(deferred, 'inflate_fast', reduced=False)
        update_target_deferrals(deferred, 'inflate_table', reduced=False)
        self.assertEqual(deferred, {'inflate_fast', 'inflate_table'})

        update_target_deferrals(deferred, 'other_target', reduced=True)
        self.assertEqual(deferred, set())


class ReviewFeedbackTest(unittest.TestCase):
    def test_unrelated_completed_work_does_not_retire_feedback(self):
        feedback = ('inflate_fast', 'keep the exported wrapper thin')
        self.assertEqual(update_review_feedback(
            feedback, 'inflate_table', report=None, completed=True), feedback)

    def test_target_completion_retires_feedback(self):
        feedback = ('inflate_fast', 'keep the exported wrapper thin')
        self.assertIsNone(update_review_feedback(
            feedback, 'inflate_fast', report=None, completed=True))

    def test_new_rejection_replaces_feedback_with_its_target(self):
        feedback = ('inflate_fast', 'old report')
        self.assertEqual(update_review_feedback(
            feedback, 'inflate_table', report='new report', completed=False),
            ('inflate_table', 'new report'))

    def test_failed_pre_review_attempt_keeps_feedback(self):
        feedback = ('inflate_fast', 'keep the exported wrapper thin')
        self.assertEqual(update_review_feedback(
            feedback, 'inflate_fast', report=None, completed=False), feedback)


class PersistentSafetyMemoryTest(unittest.TestCase):
    def test_failed_review_findings_survive_restart_recovery(self):
        with tempfile.TemporaryDirectory() as storage:
            mvir = MVIR(storage, '.')
            code = TreeNode.new(mvir, files={})
            prompt = FileNode.new(mvir, 'review prompt')
            report = FileNode.new(mvir,
                '- [P1] Keep wrapper thin — src/gzlib.rs:10-20')
            review = CodexReviewOpNode.new(
                mvir,
                old_code=code.node_id(),
                new_code=code.node_id(),
                raw_prompt=prompt.node_id(),
                report=report.node_id(),
                verdict='FAIL',
                body='review logs',
            )
            mvir.set_tag('op_history', review.node_id(), review.kind)

            self.assertEqual(prior_review_findings(mvir),
                ['Keep wrapper thin'])


if __name__ == '__main__':
    unittest.main()
