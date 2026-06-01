"""
Attempt to run GEPA prompt optimization via DSPy.

Unfortunately, running this script gives
`RuntimeError: cannot schedule new futures after shutdown`
which seems to be an error about concurrent threads in dspy

Hence, there's no point in using this script.
It is included only for completion purposes.
**To run GEPA, use the `run_gepa.py` script**.
"""

from datasets import load_dataset
import dspy
from dspy import GEPA
import os
import random

NUM_THREADS = 128

openai_api_key = os.getenv('TRACTOR_OPENAI_API_KEY')


def init_dataset():
    train_split = load_dataset("AI-MO/aimo-validation-aime")['train']
    train_split = [
        dspy.Example({
            "problem": x['problem'],
            'solution': x['solution'],
            'answer': x['answer'],
        }).with_inputs("problem")
        for x in train_split
    ]
    random.Random(0).shuffle(train_split)
    tot_num = len(train_split)

    test_split = load_dataset("MathArena/aime_2025")['train']
    test_split = [
        dspy.Example({
            "problem": x['problem'],
            'answer': x['answer'],
        }).with_inputs("problem")
        for x in test_split
    ]

    train_set = train_split[:int(0.5 * tot_num)]
    val_set = train_split[int(0.5 * tot_num):]
    test_set = test_split * 5

    return train_set, val_set, test_set


class GenerateResponse(dspy.Signature):
    """Solve the problem and provide the answer in the correct format."""
    problem = dspy.InputField()
    answer = dspy.OutputField()


def metric(example: dspy.Example, prediction: dspy.Prediction) -> int:
    correct_answer = int(example['answer'])
    try:
        llm_answer = int(prediction.answer)
    except ValueError:
        return 0
    return int(correct_answer == llm_answer)


def metric_with_feedback(
    example: dspy.Example,
    prediction: dspy.Prediction,
    trace=None, # pylint: disable=unused-argument # this argument is needed in the signature for GEPA to work
    pred_name=None, # pylint: disable=unused-argument # this argument is needed in the signature for GEPA to work
    pred_trace=None # pylint: disable=unused-argument # this argument is needed in the signature for GEPA to work
) -> dspy.Prediction:
    """
    NOTE: This is the feedback function for GEPA,. This function needs to implement strings which will provided as feedback in various cases during the working of GEPA.

    Specifically, line 235 of gepa==0.0.24 :: adapters/dspy_adapter/dspy_adapter.py calls a function which then calls this function in line 613 of dspy :: telemprompt/gepa/gepa.py.
    """
    correct_answer = int(example['answer'])
    written_solution = example.get('solution', '')
    try:
        llm_answer = int(prediction.answer)
    except ValueError:
        feedback_text = f"The final answer must be a valid integer and nothing else. You responded with '{prediction.answer}', which couldn't be parsed as a python integer. Please ensure your answer is a valid integer without any additional text or formatting."
        feedback_text += f" The correct answer is '{correct_answer}'."
        if written_solution:
            feedback_text += f" Here's the full step-by-step solution:\n{written_solution}\n\nThink about what takeaways you can learn from this solution to improve your future answers and approach to similar problems and ensure your final answer is a valid integer."
        return dspy.Prediction(score=0, feedback=feedback_text)

    score = int(correct_answer == llm_answer)

    feedback_text = ""
    if score == 1:
        feedback_text = f"Your answer is correct. The correct answer is '{correct_answer}'."
    else:
        feedback_text = f"Your answer is incorrect. The correct answer is '{correct_answer}'."
    
    if written_solution:
        feedback_text += f" Here's the full step-by-step solution:\n{written_solution}\n\nThink about what takeaways you can learn from this solution to improve your future answers and approach to similar problems."

    return dspy.Prediction(score=score, feedback=feedback_text)


if __name__ == "__main__":
    lm = dspy.LM("openai/gpt-5-mini", temperature=1, api_key=openai_api_key, max_tokens=32000)
    dspy.configure(lm=lm)
    #NOTE from Sourya: This automatically configures dspy to use lm as the language model.
    # Specifically, it puts `lm` in `dspy.dsp.utils.settings`.
    # This is then used in `dspy.predict.predict.py`.
    # The LM call happens in line 202 of `dspy.adapters.base.py`.

    train_set, val_set, test_set = init_dataset()

    program = dspy.ChainOfThought(GenerateResponse)

    evaluate = dspy.Evaluate(
        devset=test_set,
        metric=metric,
        num_threads=NUM_THREADS,
        display_table=True,
        display_progress=True
    )
    evaluate(program)

    optimizer = GEPA(
        metric=metric_with_feedback,
        auto="light",
        num_threads=NUM_THREADS,
        track_stats=True,
        reflection_minibatch_size=3,
        reflection_lm = dspy.LM(model="gpt-5", temperature=1.0, max_tokens=32000, api_key=openai_api_key)
    )
    print("==================\nOptimizing\n==================")
    optimized_program = optimizer.compile(
        program,
        trainset=train_set,
        valset=val_set,
    )
    print(f"==================\n\n{optimized_program.predict.signature.instructions}\n\n==================")
    evaluate(optimized_program)
