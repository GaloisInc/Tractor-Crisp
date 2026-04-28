from pathlib import Path

from crisp.gepa_po import do_gepa


if __name__ == '__main__':
    do_gepa(
        dataset_path = Path(__file__).resolve().parent.parent / 'Test-Corpus/Public-Tests/B01_organic',
        task_lm = 'gpt-5.4-2026-03-05',
        reflection_lm = 'gpt-5.4-2026-03-05'
    )
