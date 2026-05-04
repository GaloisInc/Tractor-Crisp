import os
from pathlib import Path

from crisp.gepa_po import do_gepa, evaluate_gepa_found_prompt


os.environ['OPENAI_API_KEY'] = os.getenv('CRISP_API_KEY') # required for GEPA


if __name__ == '__main__':

    do_gepa(
        dataset_path = Path(__file__).resolve().parent.parent / 'Test-Corpus/Public-Tests/B01_organic',
        seed_prompt_path = Path(__file__).resolve().parent.parent / 'gepa_artifacts/seed_prompt_2.txt',
        task_lm = 'gpt-5.5',
        reflection_lm = 'gpt-5.5'
    )

    for prompt_name in [
        'seed_prompt_2'
    ]:
        evaluate_gepa_found_prompt(
            dataset_path = Path(__file__).resolve().parent.parent / 'Test-Corpus/Public-Tests/B01_organic',
            prompt_path = Path(__file__).resolve().parent.parent / f'gepa_artifacts/{prompt_name}.txt',
            model = 'gpt-5.5'
        )
