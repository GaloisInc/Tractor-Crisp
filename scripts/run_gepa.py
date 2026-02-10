"""
Run GEPA prompt optimization. (Paper: https://arxiv.org/abs/2507.19457)

Note: If using the gpt-oss-20b model using Llama CPP, it has to first be downloaded in GGUF format.
The model is then run using the Python package, so `pip install`ing llama-cpp-python is required.
"""

import gepa
from pathlib import Path
import random
import os

from crisp.gepa_po import RustAdapter


# Set environment variable in the way GEPA expects
openai_api_key = os.getenv('TRACTOR_OPENAI_API_KEY')
os.environ['OPENAI_API_KEY'] = openai_api_key


def run_aime_example():
    """
    Run the example given in the GEPA README and tutorials to make sure things work as expected.
    """

    # Load AIME dataset
    trainset, valset, _ = gepa.examples.aime.init_dataset()

    seed_prompt = {
        "system_prompt": "You are a helpful assistant. You are given a question and you need to answer it. The answer should be given at the end of your response in exactly the format '### <final answer>'"
    }

    # Let's run GEPA optimization process.
    gepa_result = gepa.optimize(
        seed_candidate = seed_prompt,

        trainset = trainset,

        #NOTE from Sourya: Consider getting only a slice (e.g. `valset[:10]`) for faster performance
        valset = valset,

        #NOTE from Sourya: This is the model being optimized, i.e. the task LM
        # It is currently called in gepa/adapters/default_adapter/default_adapter.py, specifically in lines 132-137 using `litellm.batch_completion()`. This can be updated to `litellm.responses()` (or other APIs) as required by different models.
        # IMPORTANT: The main issue in `litellm.batch_completion()` is the `max_workers` argument. If this is large, the OpenAI / Anthropic / whatever server will get too many requests quickly and may return None. This will crash the program.
        # The feedback text (for both current and wrong answer cases) is hardcoded in the `ContainsAnswerEvaluator` class in the same file
        task_lm = "openai/gpt-3.5-turbo",

        #NOTE from Sourya: This is the budget, which I believe is the total number of examples being run
        # So, if this is less than the number of examples in the valset, optimization will not happen
        max_metric_calls = 150,

        #NOTE from Sourya: This is the model used for reflecting on mistakes and proposing better prompts, i.e. the reflection LM
        # The format and basic text for the reflection prompt is hardcoded in gepa/strategies/instruction_proposal.py, specifically in `InstructionProposalSignature.default_prompt_template`
        # The reflection LM is called in gepa.api.py, specifically in line 248 using `litellm.completion()`. This can be updated to `litellm.responses()` (or other APIs) as required by different models.
        reflection_lm = "openai/gpt-5"
    )

    print("GEPA Optimized Prompt:", gepa_result.best_candidate['system_prompt'])


def run_rust(
    dataset_path: str | Path,
    trainset_frac: float = 0.5,
    task_lm: str = str(Path.home() / 'Library/Caches/llama.cpp/ggml-org_gpt-oss-20b-GGUF_gpt-oss-20b-mxfp4.gguf'),
    reflection_lm: str = 'openai/gpt-5',
    max_metric_calls: int = 150
):
    """
    Run GEPA to convert unsafe Rust to safe Rust.

    Inputs:
    - dataset_path: Path to a folder containing unsafe Rust projects and .rs files inside.
    - trainset_frac: The fraction of .rs files inside `dataset_path` that will be used for training. The rest will be used for validation.
    - task_lm: The LM being optimized. Either a string, or the path to a GGUF file.
    - reflection_lm: The (usually more powerful) LM being used for reflection. Either a string, or the path to a GGUF file.
    - max_metric_calls: Required for gepa.optimize().
    """

    # Load datasets
    trainset, valset = [], []
    source_filepaths = list(Path(dataset_path).resolve().rglob('*.rs'))
    random.shuffle(source_filepaths)
    for i,source_filepath in enumerate(source_filepaths):
        with open(source_filepath, 'r', encoding='utf-8') as f:
            task_input = {
                'input': f"<code>\n{f.read()}\n</code>",
                'filepath': source_filepath
            }
        (trainset if i < trainset_frac*len(source_filepaths) else valset).append(task_input)

    adapter = RustAdapter(model = task_lm)

    gepa_result = gepa.optimize(
        seed_candidate = {
            'system_prompt': "You are an expert at converting code from unsafe Rust to safe Rust. You'll be given unsafe Rust code inside tags like this:\n<code>\nUnsafe Rust code\n</code>\nYou'll have to convert this to safe Rust. In your response, put the safe Rust code within tags as follows:\n<code>\nSafe Rust code\n</code>."
        },
        trainset = trainset,
        valset = valset,
        adapter = adapter,
        max_metric_calls = max_metric_calls,
        reflection_lm = reflection_lm
    )

    print("==================== START GEPA OPTIMIZED PROMPT ====================")
    print(gepa_result.best_candidate['system_prompt'])
    print("==================== END GEPA OPTIMIZED PROMPT ====================")


if __name__ == "__main__":
    run_rust(
        dataset_path = Path(__file__).resolve().parent.parent / 'converted_rust_projects/c2rust_Test-Corpus_B01_organic'
    )
