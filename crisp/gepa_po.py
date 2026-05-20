"""
Code for GEPA prompt optimization.

Note: This file is named gepa_po.py and not gepa.py to
avoid import issues, since the library is also called gepa.

Note: Code here is inspired from adapters/default_adapter/default_adapter.py
in the gepa package, and from https://gepa-ai.github.io/gepa/guides/adapters/
"""

import csv
from dataclasses import dataclass
import gepa
from gepa.core.adapter import EvaluationBatch, GEPAAdapter
import os
import pandas as pd
from pathlib import Path
import random
from typing import Any

from .config import Config
from .error import CrispError
from .__main__ import parse_node_id_arg
from .mvir import MVIR, TreeNode
from .workflow import Workflow


NUM_LLM_CALL_REPEATS = 3


@dataclass
class TaskInput:
    workflow: Workflow

@dataclass
class TaskTrace:
    task: TaskInput
    n_llm_input_code: TreeNode
    n_llm_output_code: TreeNode
    feedback: str

@dataclass
class TaskOutput:
    n_code: TreeNode

@dataclass
class EvaluationResult:
    score: float
    feedback: str


class ResponseEvaluator:

    def __init__(
        self,
        score_success: float = 1.0,
        score_passtests_but_unsafe: float = 0.5,
        score_compiles_but_failtests: float = 0.25,
        score_failure: float = 0.0
    ):
        self.score_success = score_success
        self.score_passtests_but_unsafe = score_passtests_but_unsafe
        self.score_compiles_but_failtests = score_compiles_but_failtests
        self.score_failure = score_failure

    def __call__(
        self,
        workflow: Workflow,
        n_llm_output_code: TreeNode,
        n_llm_input_code: TreeNode,
        n_c_code: TreeNode,
    ) -> EvaluationResult:

        # Check for correct format; if not, TreeNode hasn't changed
        if n_llm_output_code.node_id() == n_llm_input_code.node_id():
            return EvaluationResult(
                score = self.score_failure,
                feedback = "The generated response is not in the proper format."
            )

        # Check for compilation success
        compile_results = workflow.cargo_check_json_op(n_llm_output_code)
        if not compile_results.passed:
            return EvaluationResult(
                score = self.score_failure,
                feedback = f"The generated response includes Rust code that cannot compile. Please try again to produce Rust code that can compile, has identical behavior as the input, and is safe.\nHere are the results of attempting to compile:\n{compile_results.body_str()}"
            )

        # Check for tests passing
        test_results = workflow.test_op(n_llm_output_code, n_c_code)
        if not test_results.exit_code == 0:
            return EvaluationResult(
                score = self.score_compiles_but_failtests,
                feedback = f"The generated response includes Rust code that successfully compiles, but does not achieve identical behavior as the input. It fails functionality tests. Here are the outputs from the tests:\n{test_results.body_str()}\nPlease try again to produce Rust code that can compile, achieves the correct functionality by passing tests, and is safe."
            )

        # Check for un-safety
        unsafe_results = workflow.find_unsafe_op(n_llm_output_code).body_json()
        internal_unsafe_fns = []
        fns_containing_unsafe = []
        for result in unsafe_results.values():
            internal_unsafe_fns.extend(result['internal_unsafe_fns'])
            fns_containing_unsafe.extend(result['fns_containing_unsafe'])
        total_unsafe = workflow.count_unsafe(n_llm_output_code)
        if total_unsafe > 0:
            return EvaluationResult(
                score = self.score_passtests_but_unsafe,
                feedback = f"The generated response includes Rust code that successfully compiles and has identical behavior to the input (i.e. passes tests), but is unsafe. Please try again to produce safe Rust code.\nHere is some additional feedback on un-safety that might be useful:\nNumber of unsafe entities = {total_unsafe}." + (
                    f"\nInternal unsafe functions are {', '.join(internal_unsafe_fns)}."
                    if internal_unsafe_fns
                    else ""
                ) + (
                    f"\nFunctions containing unsafe are {', '.join(fns_containing_unsafe)}."
                    if fns_containing_unsafe
                    else ""
                )
            )

        # Everything works
        return EvaluationResult(
            score = self.score_success,
            feedback = "The generated response includes Rust code that successfully compiles, has identical behavior to the input (i.e. passes tests), and is safe. Good job!"
        )


class RustAdapter(GEPAAdapter[TaskInput, TaskTrace, TaskOutput]):

    def __init__(
        self,
        model: str,
        evaluator: Any = ResponseEvaluator()
    ):
        os.environ['CRISP_API_MODEL'] = model
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

            n_c_code_id = parse_node_id_arg(task['workflow'].mvir, 'c_code')
            n_c_code = task['workflow'].mvir.node(n_c_code_id)

            n_llm_input_code_id = parse_node_id_arg(task['workflow'].mvir, 'current')
            n_llm_input_code = task['workflow'].mvir.node(n_llm_input_code_id)

            for rep in range(1, NUM_LLM_CALL_REPEATS+1):
                try:
                    n_llm_output_code = task['workflow'].llm_gepa(
                        n_code = n_llm_input_code,
                        prompt = candidate['system_prompt']
                    )
                except CrispError as e:
                    if rep < NUM_LLM_CALL_REPEATS:
                        continue
                    raise e
                break

            outputs.append(TaskOutput(n_code = n_llm_output_code))

            eval_result = self.evaluator(
                workflow = task['workflow'],
                n_llm_output_code = n_llm_output_code,
                n_llm_input_code = n_llm_input_code,
                n_c_code = n_c_code
            )
            scores.append(eval_result.score)

            if capture_traces:
                trajectories.append(
                    TaskTrace(
                        task = task,
                        n_llm_input_code = n_llm_input_code,
                        n_llm_output_code = n_llm_output_code,
                        feedback = eval_result.feedback
                    )
                )

            # ================== # ================== # ================== # ================== #
            #NOTE: The following commented-out line makes the rewritten code the 'current' node
            # It is recommended to *not* do this, since this reduces the performance of GEPA
            # since the optimization goalposts are being changed by changing the 'current' node
            # Hence, keep the following line commented out
            # ================== # ================== # ================== # ================== #
            # task['workflow'].accept(n_llm_output_code)
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
        dataset = {'system_prompt': []}
        for traj in (eval_batch.trajectories or []):
            dataset['system_prompt'].append(
                {
                    "Inputs": _get_contents_of_files_matching_patterns(
                        node = traj.n_llm_input_code,
                        patterns = traj.task['workflow'].cfg.src_globs,
                        mvir = traj.task['workflow'].mvir
                    ),
                    "Generated Outputs": _get_contents_of_files_matching_patterns(
                        node = traj.n_llm_output_code,
                        patterns = traj.task['workflow'].cfg.src_globs,
                        mvir = traj.task['workflow'].mvir
                    ),
                    "Feedback": traj.feedback
                }
            )
        return dataset


def _get_contents_of_files_matching_patterns(
    node: TreeNode,
    patterns: list[str],
    mvir: MVIR,
    separator: str = '\n\n'
) -> str:
    """
    Get the contents of a node's files whose paths match given patterns.

    Inputs:
    - node: The node whose files to look at.
    - patterns: List of string path patterns. Expected to contain wildcards. Any file in `node.files` which matches any pattern *from the right* will be considered.
    - mvir: The MVIR in consideration.
    - separator: After getting matching files, extract their body contents and concatenate the strings using this separator.

    Output:
    - The concatenated string of the body contents. Example:
        Say the given `node` has files:
        ```
        {
            'a/b/c/translated_rust/src/lib.rs': <node_id_1>,
            'a/translated_rust/b/c/lib.rs': <node_id_2>,
            'a/b/translated_rust/src/b/main.rs': <node_id_2>
        }
        and `patterns` is:
        ```
        [
            'translated_rust/src/*.rs',
            'translated_rust/src/*/*.rs
        ]
        ```
        then <node_id_1> and <node_id_2> will match. Their bodies will be concatenated and returned.
    """
    file_ids = [file_id for path,file_id in node.files.items() if any(Path(path).match(f'**/{pattern}') for pattern in patterns)]
    contents = separator.join(mvir.node(file_id).body_str() for file_id in file_ids)
    return contents


def do_gepa(
    dataset_path: Path,
    seed_prompt_path: Path,
    task_lm: str = os.getenv('CRISP_API_MODEL', 'gpt-5.5'),
    reflection_lm: str = os.getenv('CRISP_API_MODEL', 'gpt-5.5'),
    trainset_frac: float = 0.5,
    max_metric_calls: int = 150,
    optimized_prompt_folder: Path = Path(__file__).parent.parent / 'gepa_artifacts/new'
):
    """
    Run GEPA optimization for converting unsafe Rust to safe Rust.

    Inputs:
    - dataset_path: Path to a corpus folder, e.g. B01_organic.
    - seed_prompt_path: Path to a text file containing the seed prompt to be used for optimization.
    - task_lm: The LM inside the loop for GEPA.
    - reflection_lm: The LM outside the loop for GEPA.
    - trainset_frac: Fraction of the data to use for training. Remaining is used for validation.
    - max_metric_calls: Required by GEPA.
    - optimized_prompt_folder: The new prompt will be saved as `prompt.txt` in this folder. Folder will be created if it doesn't exist, and will throw error if it already exists.
    """

    # Create optimized prompt folder
    optimized_prompt_folder.mkdir(parents=True, exist_ok=False)

    # Get seed prompt
    with open(seed_prompt_path, 'r', encoding='utf-8') as f:
        seed_prompt = f.read()

    # Create datasets
    trainset, valset = [], []
    project_folders = [folder for folder in dataset_path.iterdir() if folder.is_dir()]
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
    adapter = RustAdapter(model = task_lm)

    # Run GEPA optimization
    gepa_result = gepa.optimize(
        seed_candidate = {'system_prompt': seed_prompt},
        trainset = trainset,
        valset = valset,
        adapter = adapter,
        max_metric_calls = max_metric_calls,
        reflection_lm = reflection_lm
    )

    # Save optimization results
    with open(optimized_prompt_folder / 'prompt.txt', 'w', encoding='utf-8') as f:
        f.write(gepa_result.best_candidate['system_prompt'])


def run_gepa_eval_on_prompt(
    dataset_path: Path,
    optimized_prompt_folder: Path,
    model: str = os.getenv('CRISP_API_MODEL', 'gpt-5.5'),
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

    # Set model
    os.environ['CRISP_API_MODEL'] = model

    # Get prompt
    with open(optimized_prompt_folder / 'prompt.txt', 'r', encoding='utf-8') as f:
        prompt = f.read()

    # Get project folders
    project_folders = sorted(folder for folder in dataset_path.iterdir() if folder.is_dir())

    # Load response evaluator
    response_evaluator = ResponseEvaluator()

    # If it exists, read output CSV and get done files
    if output_csv_path is None:
        output_csv_path = optimized_prompt_folder / f'results_{dataset_path.name}_{model}.csv'
    output_csv_existed = False
    done_already = set()
    if output_csv_path.exists():
        output_csv_existed = True
        output_csv = pd.read_csv(output_csv_path)
        done_already = set(output_csv['filepath'])
        del output_csv

    # Write to output CSV
    with open(output_csv_path, 'a', encoding='utf-8') as csvfile:
        csvwriter = csv.writer(csvfile)

        # Write header if this is the first time output CSV is being written to
        if not output_csv_existed:
            csvwriter.writerow([
                'project_folder',
                'score'
            ])

        # Iterate
        for project_folder in project_folders:

            # Check if already done
            if project_folder.name in done_already:
                continue

            # Create mvir and workflow
            cfg = Config.from_toml_file(
                str(project_folder / 'crisp.toml'),
                mvir_storage_dir = str(project_folder / 'crisp-storage')
            )
            mvir = MVIR(cfg.mvir_storage_dir, '.')
            workflow = Workflow(cfg, mvir)

            # Get relevant nodes
            n_c_code_id = parse_node_id_arg(mvir, 'c_code')
            n_c_code = mvir.node(n_c_code_id)
            n_llm_input_code_id = parse_node_id_arg(mvir, 'current')
            n_llm_input_code = mvir.node(n_llm_input_code_id)

            # LLM rewriting
            n_llm_output_code = workflow.llm_gepa(n_code=n_llm_input_code, prompt=prompt)

            # Get score
            score = response_evaluator(
                workflow = workflow,
                n_llm_output_code = n_llm_output_code,
                n_llm_input_code = n_llm_input_code,
                n_c_code = n_c_code
            ).score

            # Write results
            csvwriter.writerow([
                project_folder.name,
                score
            ])
