"""Code for GEPA prompt optimization for agentic workflows.

Code here is inspired from adapters/default_adapter/default_adapter.py
in the gepa package, and from https://gepa-ai.github.io/gepa/guides/adapters/
"""

import csv
from dataclasses import dataclass, fields
import gepa
from gepa.core.adapter import EvaluationBatch, GEPAAdapter
import os
import pandas as pd
from pathlib import Path
import random
import traceback
from typing import Any

from . import llm_format
from .error import CrispError
from .gepa_common import (
    GEPA_MIN_SCORE,
    GEPA_MAX_SCORE,
    is_project_gepaready,
    get_workflow_for_project,
    get_bad_prompts_in_candidate,
    get_expected_formatted_blocks_from_seed_candidate
)
from .__main__ import parse_node_id_arg
from .mvir import TreeNode
from .workflow import Workflow


@dataclass
class AgentRunDetails:
    call_duration_sec: float = 0.
    output_tokens: int = 0

    @property
    def valid(self) -> bool:
        return (self.call_duration_sec != 0. and self.output_tokens != 0)


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
    run_details: dict[str, AgentRunDetails]

@dataclass
class EvaluationResult:
    score: float
    feedback: str
    safe: bool | None = None
    passtests: bool | None = None


class ResponseEvaluator:

    def __init__(
        self,
        score_safe: float = GEPA_MAX_SCORE/2,
        score_passtests: float = GEPA_MAX_SCORE/2,
        score_penalty_per_output_token: float = 1e-5,
        score_penalty_per_call_duration_sec: float = 1e-3
    ):
        self.score_safe = score_safe
        self.score_passtests = score_passtests
        self.score_penalty_per_output_token = score_penalty_per_output_token
        self.score_penalty_per_call_duration_sec = score_penalty_per_call_duration_sec

    def __call__(
        self,
        workflow: Workflow,
        n_output_code: TreeNode,
        n_input_code: TreeNode,
        n_c_code: TreeNode,
        run_details: dict[str, AgentRunDetails]
    ) -> EvaluationResult:
        score = GEPA_MIN_SCORE

        # Check if anything changed from input to output; if not, the agent failed
        if n_output_code.node_id() == n_input_code.node_id():
            return EvaluationResult(
                score = GEPA_MIN_SCORE,
                feedback = "The refactored Rust code is unchanged from the original. Please try again to produce Rust code that is safe and functionally correct."
            )

        # Check if all Codex run details make sense; if not, the agent failed
        if any(not agent_run_details.valid for agent_run_details in run_details.values()):
            return EvaluationResult(
                score = GEPA_MIN_SCORE,
                feedback = "The agent did not run correctly. Either no output tokens were generated, or the agent run is unfinished. Please try again to produce Rust code that is safe and functionally correct."
            )

        feedback_components = []
        safe = False
        passtests = False

        # Check for un-safety
        unsafe_count = workflow.count_unsafe2(n_output_code) #TODO integrate finer-grained results of types of unsafe using find_unsafe2 instead of just count_unsafe2
        if unsafe_count <= 0:
            safe = True
            score += self.score_safe
            feedback_components.append("The refactored Rust code has no unsafe entities. Good job!")
        else:
            feedback_components.append(f"The refactored Rust code has {unsafe_count} unsafe entities. Please try again to produce Rust code that is safe, and is functionally correct.")

        # Check for tests passing
        test_results = workflow.test_op(n_output_code, n_c_code)
        if test_results.exit_code == 0:
            passtests = True
            score += self.score_passtests
            feedback_components.append("The refactored Rust code passes functionality tests. Good job!")
        else:
            feedback_components.append(f"The refactored Rust code fails functionality tests. Here are the outputs from the tests:\n{test_results.body_str()}\nPlease try again to produce refactored Rust code that achieves the correct functionality by passing tests, and is safe.")

        # Penalize for output tokens
        total_output_tokens = sum(elem.output_tokens for elem in run_details.values())
        score -= (self.score_penalty_per_output_token * total_output_tokens)
        feedback_components.append(f"The refactored Rust code cost a total of {total_output_tokens} output tokens. Please try to reduce this as much as possible, while still producing Rust code that is safe and functionally correct.")

        # Penalize for call duration
        total_call_duration_sec = sum(elem.call_duration_sec for elem in run_details.values())
        score -= (self.score_penalty_per_call_duration_sec * total_call_duration_sec)
        feedback_components.append(f"The refactored Rust code took a total of {round(total_call_duration_sec)} seconds to generate. Please try to reduce this as much as possible, while still producing Rust code that is safe and functionally correct.")

        # Return final results
        score = max(score, GEPA_MIN_SCORE)
        feedback = '\n\n'.join(feedback_components)
        return EvaluationResult(
            score = score,
            feedback = feedback,
            safe = safe,
            passtests = passtests
        )


def bad_prompt_evaluator(
    expected_formatted_blocks: dict[str, set[str]],
    bad_prompt_types: set[str]
) -> EvaluationResult:
    score = GEPA_MIN_SCORE
    feedback_components = []

    for bad_prompt_type in bad_prompt_types:
        placeholders = ', '.join(expected_formatted_blocks[bad_prompt_type])
        feedback_components.append(f"'{bad_prompt_type}' either did not have placeholders {placeholders}, or had extra placeholders. This is an invalid candidate. Try again. It is VERY important that the following placeholders, and ONLY the following placeholders, are present in every candidate for '{bad_prompt_type}' -- {placeholders}.")

    feedback = '\n\n'.join(feedback_components)
    return EvaluationResult(
        score = score,
        feedback = feedback
    )


class RustAdapter(GEPAAdapter[TaskInput, TaskTrace, TaskOutput]):

    def __init__(
        self,
        evaluator: ResponseEvaluator,
        expected_formatted_blocks: dict[str, set[str]]
    ):
        self.evaluator = evaluator
        self.expected_formatted_blocks = expected_formatted_blocks

    def evaluate(
        self,
        batch: list[TaskInput],
        candidate: dict[str,str],
        capture_traces: bool = False
    ) -> EvaluationBatch[TaskTrace, TaskOutput]:

        outputs = []
        scores = []
        trajectories = [] if capture_traces else None

        bad_prompt_types = get_bad_prompts_in_candidate(
            candidate = candidate,
            expected_formatted_blocks = self.expected_formatted_blocks
        )

        # Iterate over tasks
        for task in batch:
            n_input_code = task['workflow'].mvir.node(parse_node_id_arg(task['workflow'].mvir, 'current')) #NOTE: This assumes that 'current' is the node corresponding to the non-rewritten, unsafe C2Rust output. See the docstring of `gepa_setup_initial.sh` for more details.

            # If any candidate prompt doesn't have correct formatted blocks
            if bad_prompt_types:
                eval_result = bad_prompt_evaluator(
                    expected_formatted_blocks = self.expected_formatted_blocks,
                    bad_prompt_types = bad_prompt_types
                )

                # Assign dummy values to required variables
                n_output_code = TreeNode.new(task['workflow'].mvir, files={})
                run_details = {}

            # If all candidate prompts have correct placeholders
            else:
                n_c_code = task['workflow'].mvir.node(parse_node_id_arg(task['workflow'].mvir, 'c_code'))

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

                    # ================== # ================== # ================== # ================== #
                    #NOTE: The following commented-out line makes the rewritten code the 'current' node
                    # It is recommended to *not* do this, since this reduces the performance of GEPA
                    # since the optimization goalposts are being changed by changing the 'current' node
                    # Hence, keep the following line commented out
                    # ================== # ================== # ================== # ================== #
                    # task['workflow'].accept(n_output_code)
                    # ================== # ================== # ================== # ================== #

                    n_codex = task['workflow'].mvir.node(parse_node_id_arg(task['workflow'].mvir, 'op_history'))
                    run_details = {
                        'agent_safety_prompt': AgentRunDetails(
                            call_duration_sec = n_codex.call_duration_sec,
                            output_tokens = n_codex.output_tokens
                        )
                    }

                except CrispError as e:
                    print(f'Safety attempt failed: {e}')
                    traceback.print_exc()

                    # Assign dummy values to required variables
                    n_output_code = TreeNode.new(task['workflow'].mvir, files={})
                    run_details = {
                        'agent_safety_prompt': AgentRunDetails()
                    }

                eval_result = self.evaluator(
                    workflow = task['workflow'],
                    n_output_code = n_output_code,
                    n_input_code = n_input_code,
                    n_c_code = n_c_code,
                    run_details = run_details
                )

            # Get everything required for EvaluationBatch
            outputs.append(
                TaskOutput(
                    n_code = n_output_code,
                    run_details = run_details
                )
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

        # After all tasks are done, return batch
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


def run_gepa(
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

    # Get expected formatted blocks
    expected_formatted_blocks = get_expected_formatted_blocks_from_seed_candidate(seed_prompts)

    # Create datasets
    trainset, valset = [], []
    project_folders = [folder for folder in dataset_path.iterdir() if folder.is_dir() and is_project_gepaready(folder)]
    random.shuffle(project_folders)
    for i,project_folder in enumerate(project_folders):
        workflow = get_workflow_for_project(project_folder)
        task_input = {'workflow': workflow}
        (trainset if i < trainset_frac*len(project_folders) else valset).append(task_input)

    # Instantiate GEPA adapter
    adapter = RustAdapter(
        evaluator = ResponseEvaluator(),
        expected_formatted_blocks = expected_formatted_blocks
    )

    # Run GEPA optimization
    gepa_result = gepa.optimize(
        seed_candidate = seed_prompts,
        trainset = trainset,
        valset = valset,
        adapter = adapter,
        max_metric_calls = max_metric_calls,
        reflection_lm = reflection_lm,
        perfect_score = GEPA_MAX_SCORE
    )

    # Save optimization results
    for prompt_type in prompt_types:
        with open(optimized_prompts_folder / f'{prompt_type}.txt', 'w', encoding='utf-8') as f:
            f.write(gepa_result.best_candidate[prompt_type])


def eval_gepa_prompt(
    dataset_path: Path,
    optimized_prompt_folder: Path,
    optimized_prompt_paths: dict[str, Path],
    output_csv_path: Path | None = None
):
    """
    Use the GEPA evaluation function(s) to check the performance of any prompt.

    Inputs:
    - dataset_path: Path to a corpus folder, e.g. .../B01_organic.
    - optimized_prompt_folder: Path to a folder containing the prompt to be used for evaluating inside `prompt.txt`.
    - model: The LM to run the prompt on.
    - output_csv_path: Save results to this CSV.
        - If None, set to `<optimized_prompt_folder> / results_<dataset_name>_<model>.csv`
        - File will be appended to if it already exists
    """

    # Get prompt types
    prompt_types = optimized_prompt_paths.keys()

    # Get prompts
    optimized_prompts = {}
    for prompt_type in prompt_types:
        optimized_prompts[prompt_type] = optimized_prompt_paths[prompt_type].read_text()

    # Get project folders
    project_folders = sorted(folder for folder in dataset_path.iterdir() if folder.is_dir() and is_project_gepaready(folder, plans_required=True))

    # Load response evaluator
    response_evaluator = ResponseEvaluator()

    # If it exists, read output CSV and get done files
    if output_csv_path is None:
        output_csv_path = optimized_prompt_folder / f'results_{dataset_path.name}.csv'
    output_csv_existed = False
    done_already = set()
    if output_csv_path.exists():
        output_csv_existed = True
        output_csv = pd.read_csv(output_csv_path)
        done_already = set(output_csv['project_folder'])
        del output_csv

    # Write to output CSV
    with open(output_csv_path, 'a', encoding='utf-8') as csvfile:
        csvwriter = csv.writer(csvfile)

        # Write header if this is the first time output CSV is being written to
        if not output_csv_existed:
            csvwriter.writerow(
                [
                    'project_folder',
                    'score',
                    'safe',
                    'passtests'
                ] + [
                    f'{prompt_type}_{f.name}' for prompt_type in prompt_types for f in fields(AgentRunDetails)
                ]
            )

        # Iterate
        for project_folder in project_folders:

            # Check if already done
            if project_folder.name in done_already:
                continue

            # Create workflow
            workflow = get_workflow_for_project(project_folder)

            # Get relevant nodes
            n_input_code = workflow.mvir.node(parse_node_id_arg(workflow.mvir, 'current'))
            n_c_code = workflow.mvir.node(parse_node_id_arg(workflow.mvir, 'c_code'))
            n_plans = workflow.mvir.node(parse_node_id_arg(workflow.mvir, 'plans'))

            # Run agent
            try:
                n_output_code, _ = workflow.agent_safety(
                    n_code = n_input_code,
                    n_test_code = n_c_code,
                    n_plans = n_plans,
                    agent_safety_prompt = optimized_prompts['agent_safety_prompt']
                )
                n_codex = workflow.mvir.node(parse_node_id_arg(workflow.mvir, 'op_history'))
                run_details = {
                    'agent_safety_prompt': AgentRunDetails(
                        call_duration_sec = n_codex.call_duration_sec,
                        output_tokens = n_codex.output_tokens
                    )
                }
            except CrispError as e:
                print(f'Safety attempt failed: {e}')
                traceback.print_exc()

                # Assign dummy values to required variables
                n_output_code = TreeNode.new(workflow.mvir, files={})
                run_details = {
                    'agent_safety_prompt': AgentRunDetails()
                }

            # Get evaluation result
            eval_result = response_evaluator(
                workflow = workflow,
                n_output_code = n_output_code,
                n_input_code = n_input_code,
                n_c_code = n_c_code,
                run_details = run_details
            )

            # Write results
            csvwriter.writerow(
                [
                    project_folder.name,
                    eval_result.score,
                    eval_result.safe,
                    eval_result.passtests,
                ] + [
                    getattr(run_details[prompt_type], f.name) for prompt_type in run_details.keys() for f in fields(AgentRunDetails) #NOTE: Even though we create the header row for all prompt types, we only write values for the prompt types in run_details. In practice, these two should be identical.
                ]
            )
