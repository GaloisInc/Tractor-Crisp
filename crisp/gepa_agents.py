"""Code for GEPA prompt optimization for agentic workflows.

Code here is inspired from adapters/default_adapter/default_adapter.py
in the gepa package, and from https://gepa-ai.github.io/gepa/guides/adapters/
"""

from dataclasses import dataclass
import gepa
from gepa.core.adapter import EvaluationBatch, GEPAAdapter
import os
from pathlib import Path
import random
import re
import traceback
from typing import Any

from . import llm_format
from .config import Config
from .error import CrispError
from .gepa_llm import is_project_gepaready
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
    run_details: dict[str, dict[str, Any]]

@dataclass
class EvaluationResult:
    score: float
    feedback: str


class ResponseEvaluator:

    def __init__(
        self,
        score_safe: float = 0.5,
        score_passtests: float = 0.5,
        score_penalty_per_output_token: float = 1e-5,
        score_penalty_per_call_duration_sec: float = 1e-3,
        min_score: float = 0.,
    ):
        self.score_safe = score_safe
        self.score_passtests = score_passtests
        self.score_penalty_per_output_token = score_penalty_per_output_token
        self.score_penalty_per_call_duration_sec = score_penalty_per_call_duration_sec
        self.min_score = min_score

    def __call__(
        self,
        workflow: Workflow,
        n_output_code: TreeNode,
        n_input_code: TreeNode,
        n_c_code: TreeNode,
        run_details: dict[str, Any]
    ) -> EvaluationResult:
        score = 0

        # Check if anything changed from input to output; if not, the agent failed
        if n_output_code.node_id() == n_input_code.node_id():
            return EvaluationResult(
                score = self.min_score,
                feedback = "The refactored Rust code is unchanged from the original. Please try again to produce Rust code that is safe and functionally correct."
            )

        feedback_components = []

        # Check for un-safety
        unsafe_count = workflow.count_unsafe2(n_output_code) #TODO integrate finer-grained results of types of unsafe using find_unsafe2 instead of just count_unsafe2
        if unsafe_count <= 0:
            score += self.score_safe
            feedback_components.append("The refactored Rust code has no entities that are unsafe. Good job!")
        else:
            feedback_components.append(f"The refactored Rust code has {unsafe_count} unsafe entities. Please try again to produce Rust code that is safe, and is functionally correct.")

        # Check for tests passing
        test_results = workflow.test_op(n_output_code, n_c_code)
        if test_results.exit_code == 0:
            score += self.score_passtests
            feedback_components.append("The refactored Rust code passes functaionality tests. Good job!")
        else:
            feedback_components.append(f"The refactored Rust code fails functionality tests. Here are the outputs from the tests:\n{test_results.body_str()}\nPlease try again to produce refactored Rust code that achieves the correct functionality by passing tests, and is safe.")

        # Penalize for output tokens
        total_output_tokens = sum(run_details[k].get('output_tokens', 0) for k in run_details)
        score -= (self.score_penalty_per_output_token * total_output_tokens)
        feedback_components.append(f"The refactored Rust code cost a total of {total_output_tokens} output tokens. Please try to reduce this as much as possible, while still producing Rust code that is safe and functionally correct.")

        # Penalize for call duration
        total_call_duration_sec = sum(run_details[k].get('call_duration_sec', 0) for k in run_details)
        score -= (self.score_penalty_per_call_duration_sec * total_call_duration_sec)
        feedback_components.append(f"The refactored Rust code took a total of {round(total_call_duration_sec)} seconds to generate. Please try to reduce this as much as possible, while still producing Rust code that is safe and functionally correct.")

        # Return final results
        score = max(score, self.min_score)
        feedback = '\n\n'.join(feedback_components)
        return EvaluationResult(score = score, feedback = feedback)


class RustAdapter(GEPAAdapter[TaskInput, TaskTrace, TaskOutput]):

    def __init__(
        self,
        evaluator: ResponseEvaluator,
        formatted_blocks: dict[str, set[str]]
    ):
        self.evaluator = evaluator
        self.formatted_blocks = formatted_blocks

    def evaluate(
        self,
        batch: list[TaskInput],
        candidate: dict[str,str],
        capture_traces: bool = False
    ) -> EvaluationBatch[TaskTrace, TaskOutput]:

        outputs = []
        scores = []
        trajectories = [] if capture_traces else None

        # Check if all candidate prompts have correct placeholders
        bad_prompt_types = []
        for prompt_type, formatted_blocks_for_prompt_type in self.formatted_blocks.items():
            if set(re.findall(r'\{.*\}', candidate[prompt_type])) != formatted_blocks_for_prompt_type:
                bad_prompt_types.append(prompt_type)

        # If any candidate prompt doesn't have correct placeholders, return a dummy eval batch with all scores 0
        if bad_prompt_types:
            for task in batch:
                outputs.append(None)
                scores.append(self.evaluator.min_score)
                if capture_traces:
                    feedback_components = []
                    for bad_prompt_type in bad_prompt_types:
                        placeholders = ', '.join(self.formatted_blocks[bad_prompt_type])
                        feedback_components.append(f"'{bad_prompt_type}' either did not have placeholders {placeholders}, or had extra placeholders. Please try again. It is VERY important that the following placeholders, and ONLY the following placeholders, are present in every candidate for '{bad_prompt_type}': {placeholders}.")
                    trajectories.append(
                        TaskTrace(
                            task = task,
                            n_input_code = task['workflow'].mvir.node(parse_node_id_arg(task['workflow'].mvir, 'current')),
                            n_output_code = None,
                            feedback = '\n\n'.join(feedback_components)
                        )
                    )
            return EvaluationBatch(
                outputs = outputs,
                scores = scores,
                trajectories = trajectories
            )

        # If all candidate prompts have correct placeholders, proceed with normal operation
        for task in batch:
            n_c_code = task['workflow'].mvir.node(parse_node_id_arg(task['workflow'].mvir, 'c_code'))
            n_input_code = task['workflow'].mvir.node(parse_node_id_arg(task['workflow'].mvir, 'current')) #NOTE: This assumes that 'current' is the node corresponding to the non-rewritten, unsafe C2Rust output. See the docstring of `gepa_setup_initial.sh` for more details.

            #NOTE: Any workflow method that has `fuel.use()` inside it (e.g. `workflow.do_safety_step_agent()`) will require the workflow being given fuel beforehand. If we are not calling any such method, then we don't need to give fuel. Hence, keep the following line commented out.
            # task['workflow'].fuel.give(1)

            # Try to get pre-saved plans; if they don't exist, ask the agent to generate new ones and save those
            try:
                n_plans = task['workflow'].mvir.node(parse_node_id_arg(task['workflow'].mvir, 'plans'))
            except ValueError:
                n_plans = task['workflow'].do_safety_plan_agent(
                    n_code = n_input_code,
                    n_test_code = n_c_code
                )[1]
                task['workflow'].mvir.set_tag('plans', n_plans.node_id())

            # ================== # ================== # ================== # ================== #
            #NOTE: Alternative to the above try-except block is:
            # ================== # ================== # ================== # ================== #
            # from crisp.__main__ import prior_agent_plans
            # n_plans = prior_agent_plans(task['workflow'].mvir, n_input_code)
            # if not n_plans:
            #     n_plans = task['workflow'].do_safety_plan_agent(
            #         n_code = n_input_code,
            #         n_test_code = n_c_code
            #     )[1]
            # ================== # ================== # ================== # ================== #
            # If we do the above, the agent will basically generate the plan for an example whenever it's first run in this function.
            # This is as opposed to generating the `plans` nodes for all examples via the `save_plans.py` script prior to starting a GEPA run.
            # In terms of runtime, both approaches will be similar because the agent will eventually have to generate plans for all examples, one way or another.
            # FWIW doing them using the separate script may be better because it ensures a single source of truth for all plans that is set in stone prior to starting a GEPA run. 
            # ================== # ================== # ================== # ================== #

            try:
                #TODO maybe incorporate FFI stuff and have a loop where the agent makes multiple attempts (as is done in crisp.__main__.py::safety_loop_common()), and more attempts are penalized via the evaluator
                #NOTE this may be a bad idea because each iteration takes a very long time

                n_output_code, _ = task['workflow'].agent_safety(
                    n_code = n_input_code,
                    n_test_code = n_c_code,
                    n_plans = n_plans,
                    agent_safety_prompt = candidate['agent_safety_prompt']
                )

                n_codex = task['workflow'].mvir.node(parse_node_id_arg(task['workflow'].mvir, 'op_history'))
                run_details = {
                    'agent_safety_prompt': {
                        'call_duration_sec': n_codex.call_duration_sec,
                        'output_tokens': n_codex.output_tokens
                    }
                }

            except CrispError as e:
                print(f'Safety attempt failed: {e}')
                traceback.print_exc()

            outputs.append(
                TaskOutput(
                    n_code = n_output_code,
                    run_details = run_details
                )
            )

            eval_result = self.evaluator(
                workflow = task['workflow'],
                n_output_code = n_output_code,
                n_input_code = n_input_code,
                n_c_code = n_c_code,
                run_details = run_details
            )
            scores.append(eval_result.score)

            if capture_traces:
                trajectories.append(
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
        components_to_update: list[str] # pylint: disable=unused-argument # required as per GEPA
    ) -> dict[str, list[dict[str, Any]]]:
        dataset = {'agent_safety_prompt': []}
        file_formatter = llm_format.get_file_formatter('xml')
        for traj in (eval_batch.trajectories or []):
            dataset['agent_safety_prompt'].append(
                {
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
            #NOTE: When multiple prompts are optimized together, each gets its own key-value pair in `dataset`
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
    formatted_blocks = {}
    for prompt_type in prompt_types:
        seed_prompts[prompt_type] = seed_prompt_paths[prompt_type].read_text()
        formatted_blocks[prompt_type] = set(re.findall(r'\{.*\}', seed_prompts[prompt_type]))

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
    adapter = RustAdapter(
        evaluator = ResponseEvaluator(),
        formatted_blocks = formatted_blocks
    )

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
    ...
