"""
Helper classes / functions for GEPA prompt optimization.

Note: This file is named gepa_po.py and not gepa.py to
avoid import issues, since the library is also called gepa.

Note: Code here is inspired from adapters/default_adapter/default_adapter.py
in the gepa package, and from https://gepa-ai.github.io/gepa/guides/adapters/
"""

from dataclasses import dataclass
from gepa.core.adapter import EvaluationBatch, GEPAAdapter
import json
import litellm
from llama_cpp import Llama
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any


@dataclass
class TaskInput:
    input: str
    filepath: Path

@dataclass
class TaskTrace:
    task: TaskInput
    response: str
    feedback: str

@dataclass
class TaskOutput:
    response: str

@dataclass
class EvaluationResult:
    score: float
    feedback: str


class ResponseEvaluator:

    def __init__(
        self,
        score_success: float = 1.0,
        score_compiles_but_unsafe: float = 0.5,
        score_failure: float = 0.0
    ):
        self.score_success = score_success
        self.score_compiles_but_unsafe = score_compiles_but_unsafe
        self.score_failure = score_failure

    def __call__(self, response: str) -> EvaluationResult:
        m = re.search(r'<code>\n(?P<code>.*)</code>', response, flags=re.DOTALL)
        code = m.group('code') if m else ''

        if not code:
            score = self.score_failure
            feedback = "The generated response is not in the proper format. Please include safe Rust code as follows:\n<code>\nSafe Rust code\n</code>"

        else:
            with tempfile.NamedTemporaryFile(
                suffix = '.rs',
                mode = 'w',
                encoding = 'utf-8',
                delete = False
            ) as f:
                f.write(code)
                f.flush()
                tmp_filepath = f.name

            can_compile, compile_results = self.compile(tmp_filepath)
            if not can_compile:
                score = self.score_failure
                feedback = f"The generated response includes Rust code that cannot compile. Please try again to produce Rust code that is correct and can compile, as well as safe.\nHere are the results of attempting to compile:\n{compile_results}"

            else:
                is_safe, unsafety_results = self.is_safe(tmp_filepath)
                if not is_safe:
                    score = self.score_compiles_but_unsafe
                    feedback = f"The generated response includes Rust code that successfully compiles, but is unsafe. Please try again to produce safe Rust code.\nHere is some additional feedback on un-safety that might be useful:\n{unsafety_results}"

                else:
                    score = self.score_success
                    feedback = "The generated response includes Rust code that successfully compiles, and is safe. Good job!"

            Path(tmp_filepath).unlink()

        return EvaluationResult(
            score = score,
            feedback = feedback
        )

    @staticmethod
    def compile(filepath: str) -> tuple[bool, str]:

        # Create a temporary directory to hold the rlib file
        with tempfile.TemporaryDirectory() as out_dir:
            out_dir = Path(out_dir)

            # Try binary
            r = subprocess.run(
                ["rustc", filepath, "--out-dir", out_dir],
                stdout = subprocess.PIPE,
                stderr = subprocess.PIPE,
                text = True
            )
            if r.returncode == 0:
                return True, r.stderr

            # Try library
            r = subprocess.run(
                ["rustc", "--crate-type=lib", filepath, "--out-dir", out_dir],
                stdout = subprocess.PIPE,
                stderr = subprocess.PIPE,
                text = True
            )
            if r.returncode == 0:
                return True, r.stderr

            return False, r.stderr

    @staticmethod
    def is_safe(filepath: str) -> tuple[bool, str]:
        with open(filepath, 'rb') as f:
            r = subprocess.run(
                ["cargo", "run", "--", "--single-file"],
                cwd = Path(__file__).parent.parent / 'tools/find_unsafe',
                stdin = f,
                stdout = subprocess.PIPE,
                stderr = subprocess.PIPE,
                text = True
            )
        out = json.loads(r.stdout.strip())
        internal_unsafe_fns = out['input.rs']['internal_unsafe_fns']
        fns_containing_unsafe = out['input.rs']['fns_containing_unsafe']

        output_str = ''
        if internal_unsafe_fns:
            internal_unsafe_fns = ', '.join([f'`{elem}`' for elem in internal_unsafe_fns])
            output_str += f"Internal unsafe functions are {internal_unsafe_fns}. "
        if fns_containing_unsafe:
            fns_containing_unsafe = ', '.join([f'`{elem}`' for elem in fns_containing_unsafe])
            output_str += f"Functions containing unsafe are {fns_containing_unsafe}. "

        return (not internal_unsafe_fns and not fns_containing_unsafe), output_str


class RustAdapter(GEPAAdapter[TaskInput, TaskTrace, TaskOutput]):

    def __init__(
        self,
        model: str,
        evaluator: Any = ResponseEvaluator()
    ):
        self.model = model
        self.llama_cpp_model = None if not self.model.endswith('.gguf') else Llama(
            model_path = self.model,
            n_gpu_layers = -1, # put all of model on GPU, i.e. MPS for Apple
            n_ctx = 0 # 0 = model's default
        )
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
            messages = [
                {'role': 'system', 'content': candidate['system_prompt']},
                {'role': 'user', 'content': task['input']}
            ]

            # If the Llama CPP model exists (i.e. self.model is GGUF), run that
            if self.llama_cpp_model is not None:
                response = self.llama_cpp_model.create_chat_completion(messages = messages)
                response = response['choices'][0]['message']['content']

            # Otherwise, use LiteLLM to run self.model
            else:
                response = litellm.completion(model = self.model, messages = messages)
                response = response.choices[0].message.content

            outputs.append(TaskOutput(response = response))

            eval_result = self.evaluator(response = response)
            scores.append(eval_result.score)
            if capture_traces:
                trajectories.append(
                    TaskTrace(
                        task = task,
                        response = response,
                        feedback = eval_result.feedback
                    )
                )

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
                    "Inputs": traj.task['input'],
                    "Generated Outputs": traj.response,
                    "Feedback": traj.feedback,
                }
            )

        return dataset
