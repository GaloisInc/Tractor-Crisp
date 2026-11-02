import os
from pathlib import Path

from crisp import gepa_agents, gepa_common
from crisp import skillrefiner


os.environ['OPENAI_API_KEY'] = os.getenv('CRISP_API_KEY')


def save_traces_rewards_zlib():
    skillrefiner.save_traces_rewards_for_individual_project(
        dataset_path = Path(__file__).resolve().parent.parent.parent / 'zlib',
        prompt_paths = [Path(__file__).resolve().parent.parent / f'gepa_artifacts/{prompt_name}/agent_safety_prompt.txt' for prompt_name in [
            'seed_prompts_agents',
            '20260906C_reflGPT5p6',
            '20260908_reflGPT5p6',
            '20260908B_reflGPT5p6',
        ]],
        repeats = [20,10,10,10],
    )


def eval_skillrefiner_prompt():
    """Evaluate a SkillRefiner found prompt."""
    gepa_agents.eval_gepa(

        # Set path to the folder where `zlib` exists
        dataset_path = Path(__file__).resolve().parent.parent.parent / 'zlib',

        # Leave as True
        is_individual_project = True,

        # Set path to the folder where results will be stored
        optimized_prompt_folder = Path(__file__).resolve().parent.parent / 'skillrefiner_artifacts/seed_prompt',

        # Set path to the Skillrefiner found prompt
        optimized_prompt_paths = {
            'agent_safety_prompt': Path(__file__).resolve().parent.parent / 'skillrefiner_artifacts/seed_prompt/agent_safety_prompt.txt'
        },

        # Define a response evaluator
        response_evaluator = gepa_agents.ResponseEvaluator(

            # Importance of reducing safety
            # Currently set to 1.0, so this is the only thing that's important
            score_safe = 1.0 * gepa_common.GEPA_MAX_SCORE,

            # Importance of passing tests
            # Currently set to 0.0, so this is not important
            score_passtests = 0.0 * gepa_common.GEPA_MAX_SCORE,

            # Score penalty per output token
            # Currently set to 0.0, so no penalty
            score_penalty_per_output_token = 0.,

            # Score penalty per second of wall clock time the agent runs for
            # Currently set to 0.0, so no penalty
            score_penalty_per_call_duration_sec = 0.
        ),

        # Define how many consecutive agent runs to do
        # Each run starts from the end state of the previous run
        attempts = 1,
    )


if __name__ == '__main__':
    eval_skillrefiner_prompt()
