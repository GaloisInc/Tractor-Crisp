import os
from pathlib import Path

from crisp import skillrefiner_traces
from gepa_run import response_evaluator_zlib


os.environ['OPENAI_API_KEY'] = os.getenv('CRISP_API_KEY')


def run_save_traces_and_rewards_zlib():
    skillrefiner_traces.save_traces_and_rewards_individual_project(
        dataset_path = Path(__file__).resolve().parent.parent.parent / 'zlib',
        prompt_paths = [Path(__file__).resolve().parent.parent / f'gepa_artifacts/{prompt_name}/agent_safety_prompt.txt' for prompt_name in [
            'seed_prompts_agents',
            '20260906C_reflGPT5p6',
            '20260908_reflGPT5p6',
        ]],
        repeats = 5,
        evaluator = response_evaluator_zlib,
    )


if __name__ == '__main__':
    run_save_traces_and_rewards_zlib()
