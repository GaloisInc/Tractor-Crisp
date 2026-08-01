"""
Code for GEPA prompt optimization.

Note: This file is named gepa_po.py and not gepa.py to
avoid import issues, since the library is also called gepa.

Note: Code here is inspired from adapters/default_adapter/default_adapter.py
in the gepa package, and from https://gepa-ai.github.io/gepa/guides/adapters/
"""

from dataclasses import dataclass
import gepa
from gepa.core.adapter import EvaluationBatch, GEPAAdapter
import math
import os
from pathlib import Path
import random
import traceback
from typing import Any

from . import llm_format
from .config import Config
from .error import CrispError
from .gepa_po import is_project_gepaready
from .__main__ import parse_node_id_arg
from .mvir import MVIR, TreeNode
from .workflow import Workflow


@dataclass
class TaskInput:
    workflow: Workflow

@dataclass
class TaskTrace:
    task: TaskInput
    n_input_code: TreeNode
    n_output_code: TreeNode
    feedback: str

@dataclass
class TaskOutput:
    n_code: TreeNode

@dataclass
class EvaluationResult:
    score: float
    feedback: str


def run_task(
    workflow: Workflow,
    n_code: TreeNode,
    n_c_code: TreeNode,
    prompts: dict[str, str]
):
    workflow.fuel.give(1)

    kwargs_for_do_safety_plan_agent = {}
    for kwarg in ['agent_plan_prompt', 'ffi_entry_point_rules']:
        if kwarg in prompts:
            kwargs_for_do_safety_plan_agent[kwarg] = prompts[kwarg]
    n_plans = workflow.do_safety_plan_agent(
        n_code = n_code,
        n_test_code = n_c_code,
        **kwargs_for_do_safety_plan_agent
    )[1]

    try:
        #TODO maybe incorporate a loop where the agent makes multiple attempts
        # (as is done in crisp.__main__.py::safety_loop_common()),
        # and more attempts are penalized via the evaluator
        #NOTE this may be a bad idea because each iteration takes a very long time

        #TODO maybe incorporate FFI stuff

        kwargs_for_do_safety_step_agent = {}
        for kwarg in ['agent_safety_prompt', 'agent_ffi_review_prompt', 'ffi_entry_point_rules']:
            if kwarg in prompts:
                kwargs_for_do_safety_step_agent[kwarg] = prompts[kwarg]
        n_new_code, _, _ = workflow.do_safety_step_agent(
            n_code = n_code,
            n_test_code = n_c_code,
            n_plans = n_plans,
            **kwargs_for_do_safety_step_agent
        )

        if n_new_code is not None:
            n_code = n_new_code

    except CrispError as e:
        print(f'Safety attempt failed: {e}')
        traceback.print_exc()

    return n_code


class ResponseEvaluator:

    def __init__(
        self,
        scores: dict[str, float] = {
            'safe': 0.8,
            'passtests': 0.2,
            'failure': 0.0
        },
        score_penalty_per_attempt: float = 0.1
    ):
        assert math.isclose(sum(scores.values()), 1), "Values of `scores` dictionary doesn't sum to 1"
        self.score_safe = scores['safe']
        self.score_passtests = scores['passtests']
        self.score_failure = scores['failure']
        self.score_penalty_per_attempt = score_penalty_per_attempt
        #TODO add scores and penalties for tokens used

    def __call__(
        self,
        workflow: Workflow,
        n_output_code: TreeNode,
        n_input_code: TreeNode,
        n_c_code: TreeNode,
        attempts: int = 1
    ) -> EvaluationResult:
        score = 0
        feedback = ""

        # Check if anything changed from input to output; if not, the agent failed
        if n_output_code.node_id() == n_input_code.node_id():
            return EvaluationResult(
                score = self.score_failure,
                feedback = "The refactored Rust code is unchanged from the original. Please try again to produce Rust code that is safe and functionally correct."
            )

        # Check for un-safety
        unsafe_count = workflow.count_unsafe2(n_output_code)
        if unsafe_count <= 0:
            score += self.score_safe
        else:
            feedback += f"The refactored Rust code has {unsafe_count} entities that are unsafe. Please try again to produce Rust code that is safe, and is functionally correct."

        # Check for tests passing
        test_results = workflow.test_op(n_output_code, n_c_code)
        if test_results.exit_code == 0:
            score += self.score_passtests
        else:
            feedback += f"The refactored Rust code does not achieve identical behavior as the input. It fails functionality tests. Here are the outputs from the tests:\n{test_results.body_str()}\nPlease try again to produce refactored Rust code that achieves the correct functionality by passing tests, and is safe."

        # Check attempts
        score -= (self.score_penalty_per_attempt * (attempts-1)) # 1 attempt is fine, we penalize beyond that
        if attempts > 1:
            feedback += f"The refactored Rust code was arrived at after {attempts} attempts. Please try to produce safe and functionally correct Rust code in fewer attempts."

        # Return final results
        score = max(score, 0) # since score should be non-negative (I think)
        return EvaluationResult(score = score, feedback = feedback)


class RustAdapter(GEPAAdapter[TaskInput, TaskTrace, TaskOutput]):

    def __init__(
        self,
        evaluator: Any = ResponseEvaluator()
    ):
        self.evaluator = evaluator

    def evaluate(
        self,
        batch: list[TaskInput],
        candidate: dict[str,str],
        capture_traces: bool = False
    ) -> EvaluationBatch[TaskTrace, TaskOutput]:

        outputs = []
        scores = []
        trajectories = [] if capture_traces else None

        for task in batch:
            n_c_code = task['workflow'].mvir.node(parse_node_id_arg(task['workflow'].mvir, 'c_code'))
            n_input_code = task['workflow'].mvir.node(parse_node_id_arg(task['workflow'].mvir, 'current')) #NOTE: This assumes that 'current' is the node corresponding to the non-rewritten, unsafe C2Rust output. See the docstring of `gepa_setup_initial.sh` for more details.

            n_output_code = run_task(
                workflow = task['workflow'],
                n_code = n_input_code,
                n_c_code = n_c_code,
                prompts = candidate #TODO how do we ensure that candidate prompts always have the proper {} portions to be formatted (e.g. `agent_plan_prompt` should always have `{cargo_dir_path}`)? Maybe the solution is to eliminate all such blocks from the GEPA seed prompts.
            )

            outputs.append(TaskOutput(n_code = n_output_code))

            eval_result = self.evaluator(
                workflow = task['workflow'],
                n_output_code = n_output_code,
                n_input_code = n_input_code,
                n_c_code = n_c_code
            )
            scores.append(eval_result.score)

            if capture_traces:
                trajectories.append( #TODO capture more feedback for specific prompt types
                    TaskTrace(
                        task = task,
                        n_input_code = n_input_code,
                        n_output_code = n_output_code,
                        feedback = eval_result.feedback
                    )
                )

            # ================== # ================== # ================== # ================== #
            #NOTE: The following commented-out line makes the rewritten code the 'current' node
            # It is recommended to *not* do this, since this reduces the performance of GEPA
            # since the optimization goalposts are being changed by changing the 'current' node
            # Hence, keep the following line commented out
            # ================== # ================== # ================== # ================== #
            # task['workflow'].accept(n_output_code)
            # ================== # ================== # ================== # ================== #

        print("==================== RETURNING EVALUATION BATCH ====================")
        return EvaluationBatch(
            outputs = outputs,
            scores = scores,
            trajectories = trajectories
        )

    def make_reflective_dataset(
        self,
        candidate: dict[str,str], # pylint: disable=unused-argument # required as per GEPA
        eval_batch: EvaluationBatch[TaskTrace, TaskOutput],
        components_to_update: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        dataset = {}
        file_formatter = llm_format.get_file_formatter('xml')

        for component_name in components_to_update:
            component_data = []

            for traj in (eval_batch.trajectories or []):
                component_data.append(
                    { #TODO get individual input-outputs or similar for specific prompt types
                        "Inputs": file_formatter.emit_files(
                            mvir = traj.task['workflow'].mvir,
                            n = traj.n_input_code,
                            glob_filter = traj.task['workflow'].cfg.src_globs
                        )[0],
                        "Generated Outputs": file_formatter.emit_files(
                            mvir = traj.task['workflow'].mvir,
                            n = traj.n_output_code,
                            glob_filter = traj.task['workflow'].cfg.src_globs
                        )[0],
                        "Feedback": traj.feedback
                    }
                )

            dataset[component_name] = component_data

        return dataset


def do_gepa(
    dataset_path: Path,
    seed_prompt_paths: dict[str, Path],
    reflection_lm: str = os.getenv('CRISP_API_MODEL', 'gpt-5.6-sol'),
    trainset_frac: float = 0.5,
    max_metric_calls: int = 150,
    optimized_prompts_folder: Path = Path(__file__).parent.parent / 'gepa_artifacts/new'
):
    """
    Run GEPA optimization for converting unsafe Rust to safe Rust.

    Inputs:
    - dataset_path: Path to a corpus folder, e.g. B01_organic.
    - reflection_lm: The LM outside the loop for GEPA.
    - trainset_frac: Fraction of the data to use for training. Remaining is used for validation.
    - max_metric_calls: Required by GEPA.
    - optimized_prompts_folder: The new prompt will be saved as `prompt.txt` in this folder. Folder will be created if it doesn't exist, and will throw error if it already exists.
    """

    # Get prompt types being optimized
    prompt_types = seed_prompt_paths.keys()

    # Create optimized prompts folder
    optimized_prompts_folder.mkdir(parents=True, exist_ok=False)

    # Get seed prompts
    seed_prompts = {}
    for prompt_type in prompt_types:
        seed_prompts[prompt_type] = seed_prompt_paths[prompt_type].read_text()

    # Create datasets
    trainset, valset = [], []
    project_folders = [folder for folder in dataset_path.iterdir() if folder.is_dir() and is_project_gepaready(folder)]
    random.shuffle(project_folders)
    for i,project_folder in enumerate(project_folders):
        cfg = Config.from_toml_file(
            str(project_folder / 'crisp.toml'),
            mvir_storage_dir = str(project_folder / 'crisp-storage')
        )
        mvir = MVIR(cfg.mvir_storage_dir, '.')
        workflow = Workflow(cfg, mvir)
        task_input = {'workflow': workflow}
        (trainset if i < trainset_frac*len(project_folders) else valset).append(task_input)

    # Instantiate GEPA adapter
    adapter = RustAdapter()

    # Run GEPA optimization
    gepa_result = gepa.optimize(
        seed_candidate = seed_prompts,
        trainset = trainset,
        valset = valset,
        adapter = adapter,
        max_metric_calls = max_metric_calls,
        reflection_lm = reflection_lm
    )

    # Save optimization results
    for prompt_type in prompt_types:
        with open(optimized_prompts_folder / f'{prompt_type}.txt', 'w', encoding='utf-8') as f:
            f.write(gepa_result.best_candidate[prompt_type])


def run_gepa_eval_on_prompt():
    #TODO
    ...
