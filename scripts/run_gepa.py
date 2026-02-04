import gepa
import os


openai_api_key = os.getenv('TRACTOR_OPENAI_API_KEY')
os.environ['OPENAI_API_KEY'] = openai_api_key


def run_aime_example():

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

        #NOTE from Sourya: This is the model being optimized, i.e. the 'junior' LM
        # It is currently called in gepa/adapters/default_adapter/default_adapter.py, specifically in lines 132-137 using `litellm.batch_completion()`. This can be updated to `litellm.responses()` (or other APIs) as required by different models.
        # IMPORTANT: The main issue in `litellm.batch_completion()` is the `max_workers` argument. If this is large, the OpenAI / Anthropic / whatever server will get too many requests quickly and may return None. This will crash the program.
        # The feedback text (for both current and wrong answer cases) is hardcoded in the `ContainsAnswerEvaluator` class in the same file
        task_lm = "openai/gpt-3.5-turbo",

        #NOTE from Sourya: This is the budget, which I believe is the total number of examples being run
        # So, if this is less than the number of examples in the valset, optimization will not happen
        max_metric_calls = 150,

        #NOTE from Sourya: This is the model used for reflecting on mistakes and proposing better prompts, i.e. the 'senior' LM
        # The format and basic text for the reflection prompt is hardcoded in gepa/strategies/instruction_proposal.py, specifically in `InstructionProposalSignature.default_prompt_template`
        # The senior LM is called in gepa.api.py, specifically in line 248 using `litellm.completion()`. This can be updated to `litellm.responses()` (or other APIs) as required by different models.
        reflection_lm = "openai/gpt-5"
    )

    print("GEPA Optimized Prompt:", gepa_result.best_candidate['system_prompt'])


def run_crisp():
    pass


if __name__ == "__main__":
    run_aime_example()
