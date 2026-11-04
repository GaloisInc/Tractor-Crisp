from contextlib import contextmanager
from datetime import datetime
import functools

from .error import CrispError
from .mvir import TreeNode, BranchStateNode, BranchStepNode, Untyped
from .planning import StepPlan, StepGoal
from .rand_target import PickTarget
from .util import FuelLimits
from .workflow import Workflow, FuelCounter, OutOfFuelError, AgentTargetOther


def step(f):
    """
    Wrapper for mutating operations on `BranchState`.  Records the state before
    and after in a `BranchStepNode`.  If the operation throws an exception,
    this automatically reverts to the initial state.
    """

    @functools.wraps(f)
    def g(self, *args, **kwargs):
        start_node = self._snapshot()
        self._n = start_node

        start_time = datetime.now()

        old_child_steps = self._child_steps
        self._child_steps = []

        end_node = None
        fail_msg = None
        try:
            f(self, *args, **kwargs)
            end_node = self._snapshot()
            self._n = end_node
        except BaseException as e:
            # Catch `BaseException` rather than `Exception` so we include a
            # message for `KeyboardInterrupt` (which doesn't inherit from
            # `Exception`).
            fail_msg = f'{type(e).__name__}: {e}'
            raise
        finally:
            if end_node is None:
                self._reset(start_node)
            end_time = datetime.now()

            new_child_steps = self._child_steps
            self._child_steps = old_child_steps
            step_node = BranchStepNode.new(
                self.mvir,
                name = f.__name__,
                old_state = start_node.node_id(),
                new_state = end_node.node_id() if end_node is not None else None,
                fail_msg = fail_msg,
                children = [step.node_id() for step in new_child_steps],
                start_time = start_time,
                end_time = end_time,
            )

            self._child_steps.append(step_node)

            desc = f'branch step {f.__name__}' + ('' if fail_msg is None else f' (failed)')
            print(f'*** completed {desc} ({step_node.node_id()})')
            self.mvir.set_tag('branch_op_history', step_node.node_id(), desc)
    return g

class BranchState:
    def __init__(self, w: Workflow, n: BranchStateNode, limits: FuelLimits):
        self._w = w
        self.mvir = w.mvir
        self._limits = limits
        self._child_steps = []

        self._reset(n)

        # Fuel is not persisted in MVIR, so limits can be changed between runs.
        self.safety_tries_fuel = FuelCounter('safety_tries',
                default_give = self._limits.safety_tries)

    def _reset(self, n: BranchStateNode):
        self._n = n

        fields = n.fields.raw

        self.c_code = self.mvir.node(fields['c_code'])
        self.code = self.mvir.node(fields['code'])
        self.plans = [StepPlan.from_cbor(x) for x in fields.get('plans', ())]

        # Sanity check: `__init__` and `_snapshot` should be inverses.
        assert n.node_id() == self._snapshot().node_id()

    def _snapshot(self) -> BranchStateNode:
        fields = dict(
            c_code = self.c_code.node_id(),
            code = self.code.node_id(),
            plans = [mp.to_cbor() for mp in self.plans],
        )
        return BranchStateNode.new(self.mvir, fields = Untyped(fields))

    @classmethod
    def new(cls, w: Workflow, limits: FuelLimits, n_c_code: TreeNode, n_code: TreeNode):
        n = BranchStateNode.new(w.mvir, fields = Untyped(dict(
            c_code = n_c_code.node_id(),
            code = n_code.node_id(),
            plans = [],
        )))
        return cls(w, n, limits)


    def accept(self):
        self._w.accept(self.code)
        self.mvir.set_tag('current_branch', self._n.node_id(),
            f'safety_loop {self.safety_tries_fuel.fuel}')


    @step
    def agent_safety_unchecked(self, target):
        self.safety_tries_fuel.use()
        self.code, _ = self._w.agent_safety(self.code, self.c_code, None,
            target_goal = target)

    @step
    def agent_safety(self, target):
        old_code = self.code

        self.agent_safety_unchecked(target)

        if self.code is None:
            raise CrispError("agent failed to produce any code")

        op_test = self._w.test_op(self.code, self.c_code)
        if op_test.exit_code != 0:
            raise CrispError("new code failed the tests", op_test)

        old_unsafe_json = self._w.find_unsafe2_json(old_code)
        op_unsafe = self._w.check_unsafe2_op(self.code, old_unsafe_json)
        if op_unsafe.exit_code != 0:
            raise CrispError("new code increased unsafety", op_unsafe)

        op_ffi_review = self._w.ffi_review_op(old_code, self.code)
        if op_ffi_review is not None and not op_ffi_review.passed:
            raise CrispError("new code failed FFI review", op_ffi_review)

    @step
    def update_plans(self):
        if len(self.plans) == 0:
            plan_all = StepPlan(
                description = "Entire codebase",
                goals = [
                    StepGoal(key = 'total_unsafe'),
                    StepGoal(key = 'field_contains_raw_ptr'),
                ],
            )
            unsafe_json = self._w.find_unsafe2_json_dicts(self.code)
            if plan_all.count(self.mvir, unsafe_json) == 0:
                # All unsafety detectable by `StepGoal` has been removed.
                print('planning: all plannable unsafety has been removed')
                return

            detail, plans = self._w.plan_step_init(self.code)
            for plan in plans:
                plan.details = [detail.node_id()]
            self.plans = plans

            self.accept()

        # Subdivide the first plan until it's small enough to attempt as a
        # single unit.
        while len(self.plans) > 0:
            p = self.plans[0]

            unsafe_json = self._w.find_unsafe2_json_dicts(self.code)

            if p.count_excess(self.mvir, unsafe_json) == 0:
                print(f'planning: complete (zero excess unsafe): {p.description!r}')
                del self.plans[0]
                continue

            # Don't retry clusters that we previously failed to subdivide
            # several times.  Let the rewriter work on the cluster as a single
            # unit instead.
            if p.subdiv_attempts >= 3:
                print(f'planning: too many subdiv attempts ({p.subdiv_attempts}): '
                    f'{p.description!r}')
                break

            m = p.measure(self.mvir, unsafe_json)
            if m['functions'] + m['types'] <= 4:
                # Don't try to subdivide further if there are only a few items.
                break
            if m['lines'] <= 2000:
                # Small enough to attempt as a single unit.
                break


            # Subdivide the plan at the head of the list.
            detail, subplans = self._w.plan_step_subdivide(
                self.code, p,
                relevant_code = p.relevant_code(self.mvir, unsafe_json),
            )

            if len(subplans) == 0:
                # Agent returned `[]`, indicating that it can't reasonably
                # subdivide `p`.  Mark `p` as indivisible (by setting a very
                # large `subdiv_attempts`) and bail out.  The rewriter will try
                # to carry out plan `p` as a single unit.
                p.subdiv_attempts += 1000
                break
            else:
                # Record a subdivision attempt.  If the new sub-steps fail to
                # solve `p.goals`, we may return to `p` later and try to
                # subdivide it again, but only up to a limit on number of
                # attempts.
                p.subdiv_attempts += 1

            for subplan in subplans:
                subplan.details = p.details + [detail.node_id()]
            # Prepend the new subplans, while keeping the current plan in
            # place.  If the subplans together cover the original plan, then
            # once the subplans are complete, the original plan will be
            # discarded as already done.  If they don't fully cover the
            # original plan, we'll re-attempt it (re-subdividing if necessary).
            self.plans = subplans + self.plans

            self.accept()

        print(f'planning: final plans ({len(self.plans)}):')
        for i, p in enumerate(self.plans):
            print(f'{i}: {p!r}')

    @step
    def safety_loop(self):

        pick_target = PickTarget(self._limits)

        consecutive_failure_fuel = FuelCounter('consecutive failures',
            default_give = self._limits.max_consecutive_failures)

        try:
            while True:
                old_fuel = self.safety_tries_fuel.fuel
                old_unsafe_count = self._w.count_unsafe2(self.code)

                if old_unsafe_count == 0:
                    # Success!
                    break

                self.update_plans()
                # TODO: actually use the plans, instead of random targeting

                target = pick_target.current_target_goal(self._w, self.code)

                try:
                    self.agent_safety(target)
                except CrispError:
                    # Keep trying as long as fuel remains.  Fuel will be reduced
                    # below because the failed step was reverted, leaving the
                    # unsafe count unchanged.
                    pass

                self.accept()

                new_unsafe_count = self._w.count_unsafe2(self.code)
                if new_unsafe_count < old_unsafe_count:
                    # Success - refill `consecutive_failure_fuel`.
                    consecutive_failure_fuel.give()
                else:
                    # Failure - consume fuel, and bail out if none is left.
                    consecutive_failure_fuel.use()

                assert self.safety_tries_fuel.fuel < old_fuel, \
                    'safety loop ran without consuming fuel (infinite loop)'

        except OutOfFuelError as e:
            print(f'stopping due to lack of fuel: {e}')
