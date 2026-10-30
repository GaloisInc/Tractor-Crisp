import os
from pathlib import Path

from crisp import skillrefiner_traces


os.environ['OPENAI_API_KEY'] = os.getenv('CRISP_API_KEY')


def run_save_traces_zlib():
    skillrefiner_traces.save_traces_individual_project(
        prompt_paths = [Path(__file__).resolve().parent.parent / f'gepa_artifacts/{prompt_name}/agent_safety_prompt.txt' for prompt_name in [
            'seed_prompts_agents',
            '20260906C_reflGPT5p6',
            '20260908_reflGPT5p6',
        ]],
        dataset_path = Path(__file__).resolve().parent.parent.parent / 'zlib',
        repeats = 5,
    )


if __name__ == '__main__':
    run_save_traces_zlib()
