"""
Use the GEPA prompt optimization technique.
https://arxiv.org/abs/2507.19457

Note: This file is named gepa_po.py and not gepa.py to
avoid import issues, since the library is also called gepa.

Note: Code here is inspired from adapters/default_adapter/default_adapter.py
in the gepa package, and from https://gepa-ai.github.io/gepa/guides/adapters/
"""

from dataclasses import dataclass
from gepa.core.adapter import EvaluationBatch, GEPAAdapter
from openai import OpenAI
import re
import tempfile
from typing import Any


@dataclass
class TaskInput:
    input: str

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

    def __init__(self, failure_score: float = 0.0):
        self.failure_score = failure_score

    def __call__(self, response: str) -> EvaluationResult:
        code = ''

        for output in response.output:
            if output.content is None:
                continue
            for content in output.content:
                if content.type != 'output_text':
                    continue
                m = re.search(r'<code>\n(?P<code>.*)</code>', content.text, flags=re.DOTALL)
                if m:
                    code = m.group('code')

        if not code:
            score = self.failure_score
            feedback = "The generated response is not in the proper format. Please include safe Rust code as follows:\n<code>\nSafe Rust code goes here\n</code>"

        else:
            with tempfile.NamedTemporaryFile(
                suffix = '.rs',
                mode = 'w',
                encoding = 'utf-8'
            ) as f:
                f.write(code)
                #TODO evaluate the Rust file produced in f.name, then
                # replace the following lines with appopriate score and feedback
                # can also add objective scores, e.g. logical errors, compiler errors, etc
                score = self.failure_score
                feedback = "The generated response is ..."

        return EvaluationResult(
            score = score,
            feedback = feedback
        )


class RustAdapter(GEPAAdapter[TaskInput, TaskTrace, TaskOutput]):

    def __init__(
        self,
        model: str,
        client: OpenAI, #TODO extend to other clients and APIs like Huggingface (can use litellm)
        evaluator: Any = ResponseEvaluator()
    ):
        self.model = model
        self.client = client
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

        responses = [
            self.client.responses.create(
                model = self.model,
                instructions = candidate['system_prompt'],
                input = task.input
            )
            for task in batch
        ]

        for task, response in zip(batch, responses, strict=True):
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
        components_to_update: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        dataset = {'system_prompt': []}

        for traj in (eval_batch.trajectories or []):
            dataset['system_prompt'].append(
                {
                    "Inputs": traj.task.input,
                    "Generated Outputs": traj.response,
                    "Feedback": traj.feedback,
                }
            )

        return dataset
