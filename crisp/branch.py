from contextlib import contextmanager
from datetime import datetime
import functools

from .error import CrispError
from .mvir import BranchStateNode, BranchStepNode, Untyped
from .rand_target import PickTarget
from .util import FuelLimits
from .workflow import Workflow, FuelCounter, AgentTargetOther


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
        plans_id = fields.get('plans')
        self.plans = self.mvir.node(plans_id) if plans_id is not None else None

        # Sanity check: `__init__` and `_snapshot` should be inverses.
        assert n.node_id() == self._snapshot().node_id()

    def _snapshot(self) -> BranchStateNode:
        fields = dict(
            c_code = self.c_code.node_id(),
            code = self.code.node_id(),
        )
        if self.plans is not None:
            fields['plans'] = self.plans.node_id()
        return BranchStateNode.new(self.mvir, fields = Untyped(fields))

    @classmethod
    def new(cls, w: Workflow, limits: FuelLimits, n_c_code: TreeNode, n_code: TreeNode):
        n = BranchStateNode.new(w.mvir, fields = Untyped(dict(
            c_code = n_c_code.node_id(),
            code = n_code.node_id(),
        )))
        return cls(w, n,limits)


    def accept(self):
        self._w.accept(self.code)
        self.mvir.set_tag('current_branch', self._n.node_id(),
            f'safety_loop {self.safety_tries_fuel.fuel}')


    @step
    def agent_safety_unchecked(self, target):
        self.safety_tries_fuel.use()
        self.code, self.plans = self._w.agent_safety(self.code, self.c_code, self.plans,
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

    @step
    def init_plans(self):
        _, self.plans = self._w.do_safety_plan_agent(self.code, self.c_code)

    @step
    def safety_loop(self):
        if self.plans is None:
            self.init_plans()

        pick_target = PickTarget(self._limits)

        consecutive_failure_fuel = FuelCounter('consecutive failures',
            default_give = self._limits.max_consecutive_failures)

        while not self.safety_tries_fuel.is_empty():
            old_fuel = self.safety_tries_fuel.fuel
            old_unsafe_count = self._w.count_unsafe2(self.code)

            target = pick_target.current_target_goal(self._w, self.code)

            try:
                self.agent_safety(target)
                consecutive_failure_fuel.give()
            except CrispError:
                consecutive_failure_fuel.use()
                # Keep trying as long as fuel remains.
                pass

            self.accept()

            new_unsafe_count = self._w.count_unsafe2(self.code)
            if new_unsafe_count < old_unsafe_count:
                # Success - refill `consecutive_failure_fuel`.
                consecutive_failure_fuel.give()
            else:
                consecutive_failure_fuel.use()

            assert self.safety_tries_fuel.fuel < old_fuel, \
                'safety loop ran without consuming fuel (infinite loop)'

