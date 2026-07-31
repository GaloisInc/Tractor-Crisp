import os
from pathlib import Path

from crisp.gepa_po import do_gepa, run_gepa_eval_on_prompt
from crisp.gepa_agents import do_gepa as do_gepa_agents


os.environ['OPENAI_API_KEY'] = os.getenv('CRISP_API_KEY') # required for GEPA


def run_gepa_llm():
    """
    Single prompt GEPA optimization using individual LLMs.
    """
    do_gepa(
        dataset_path = Path(__file__).resolve().parent.parent / 'Test-Corpus/Public-Tests/B01_organic',
        seed_prompt_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/seed_prompt_2/prompt.txt',
        task_lm = 'gpt-5.5',
        reflection_lm = 'gpt-5.5'
    )

def evaluate_gepa_llm():
    """
    Use the GEPA evaluation function(s) to check the performance of any prompt using individual LLMs.
    """
    for prompt_name in [ # insert prompt names for evaluation here (see examples below)
        # '20260616_taskGPT5p5_reflGPT5p5',
        # 'seed_prompt_2',
        # ...
    ]:
        for dataset_name in [ # insert names of datasets to be evaluated on here (see examples below)
            'B01_organic',
            'B01_synthetic',
            'B02_organic',
            'B02_synthetic',
            # ...
        ]:
            run_gepa_eval_on_prompt(
                dataset_path = Path(__file__).resolve().parent.parent / f'Test-Corpus/Public-Tests/{dataset_name}',
                optimized_prompt_folder = Path(__file__).resolve().parent.parent / f'gepa_artifacts/{prompt_name}',
                model = 'gpt-5.5'
            )


def run_gepa_agents():
    """
    Multi-prompt GEPA optimization using agents.
    """
    do_gepa_agents(
        dataset_path = Path(__file__).resolve().parent.parent / 'Test-Corpus/Public-Tests/B01_organic',
        seed_prompt_paths = {
            'agent_plan_prompt': Path(__file__).resolve().parent.parent / 'gepa_artifacts/seed_prompts_agents/agent_plan_prompt.md',
            # 'agent_safety_prompt': Path(__file__).resolve().parent.parent / 'gepa_artifacts/seed_prompts_agents/agent_safety_prompt.txt',
            'agent_ffi_review_prompt': Path(__file__).resolve().parent.parent / 'gepa_artifacts/seed_prompts_agents/agent_ffi_review_prompt.md',
            'ffi_entry_point_rules': Path(__file__).resolve().parent.parent / 'gepa_artifacts/seed_prompts_agents/ffi_entry_point_rules.md',
        },
        reflection_lm = 'gpt-5.6-sol'
    )


if __name__ == '__main__':

    # run_gepa_llm()
    # evaluate_gepa_llm()

    run_gepa_agents()
