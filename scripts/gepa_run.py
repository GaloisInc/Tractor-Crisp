import os
from pathlib import Path

from crisp import gepa_llm, gepa_agents, gepa_common


os.environ['OPENAI_API_KEY'] = os.getenv('CRISP_API_KEY') # required for GEPA

response_evaluator_test_corpus = gepa_agents.ResponseEvaluator(
    score_safe = 0.5 * gepa_common.GEPA_MAX_SCORE,
    score_passtests = 0.5 * gepa_common.GEPA_MAX_SCORE
)
response_evaluator_zlib = gepa_agents.ResponseEvaluator(
    score_safe = 1.0 * gepa_common.GEPA_MAX_SCORE,
    score_passtests = 0.0 * gepa_common.GEPA_MAX_SCORE,
    score_penalty_per_output_token = 0.,
    score_penalty_per_call_duration_sec = 0.
)


def run_gepa_llm():
    """Single prompt GEPA optimization using individual LLMs."""
    gepa_llm.run_gepa(
        dataset_path = Path(__file__).resolve().parent.parent / 'Test-Corpus/Public-Tests/B01_organic',
        seed_prompt_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/seed_prompt_2/prompt.txt',
        task_lm = 'gpt-5.5',
        reflection_lm = 'gpt-5.5'
    )

def evaluate_gepa_llm():
    """Use the GEPA evaluation function(s) to check the performance of any prompt using individual LLMs."""
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
            gepa_llm.eval_gepa_prompt(
                dataset_path = Path(__file__).resolve().parent.parent / f'Test-Corpus/Public-Tests/{dataset_name}',
                optimized_prompt_folder = Path(__file__).resolve().parent.parent / f'gepa_artifacts/{prompt_name}',
                model = 'gpt-5.5'
            )


def run_gepa_agents():
    """GEPA optimization using agents."""
    gepa_agents.run_gepa(
        dataset_path = Path(__file__).resolve().parent.parent.parent / 'zlib',
        is_individual_project = True,
        seed_prompt_paths = {
            'agent_safety_prompt': Path(__file__).resolve().parent.parent / 'gepa_artifacts/seed_prompts_agents/agent_safety_prompt.txt'
        },
        reflection_lm = 'gpt-5.6-sol',
        response_evaluator = response_evaluator_zlib
    )


def evaluate_gepa_agents():
    """Use the GEPA evaluation function(s) to check the performance of a set of prompts using agents."""
    for prompt_name in [ # insert prompt names for evaluation here (see examples below)
        'seed_prompts_agents',
        '20260906_reflGPT5p6',
        '20260906B_reflGPT5p6',
        '20260906C_reflGPT5p6',
        '20260908_reflGPT5p6',
        '20260908B_reflGPT5p6',
        '20260908C_reflGPT5p6',
    ]:
        attempts = 20
        gepa_agents.eval_gepa_prompt(
            dataset_path = Path(__file__).resolve().parent.parent.parent / 'zlib',
            is_individual_project = True,
            optimized_prompt_folder = Path(__file__).resolve().parent.parent / f'gepa_artifacts/{prompt_name}',
            optimized_prompt_paths = {
                'agent_safety_prompt': Path(__file__).resolve().parent.parent / f'gepa_artifacts/{prompt_name}/agent_safety_prompt.txt'
            },
            response_evaluator = response_evaluator_zlib,
            attempts = attempts,
            save_final_attempt_node_name = f"attempt{attempts}_{prompt_name}"
        )


if __name__ == '__main__':

    # run_gepa_llm()
    # evaluate_gepa_llm()

    # run_gepa_agents()
    # evaluate_gepa_agents()

    pass
