import os
from pathlib import Path

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


if __name__ == '__main__':
    save_traces_rewards_zlib()
