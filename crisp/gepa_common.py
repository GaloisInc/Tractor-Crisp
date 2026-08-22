"""Common code for GEPA prompt optimization."""

from pathlib import Path
import re

from .config import Config
from .mvir import MVIR
from .workflow import Workflow


GEPA_MIN_SCORE = 0.
GEPA_MAX_SCORE = 1.


def is_project_gepaready(project_folder: Path, plans_required: bool = False) -> bool:
    """
    Given a project folder, check if it has the required files to run GEPA and return True / False accordingly.
    If `plans_required` is set, the required files also include the `plans` node.
    """
    res = True
    required_files = [
        project_folder / 'crisp.toml',
        project_folder / 'crisp-storage/tags/c_code',
        project_folder / 'crisp-storage/tags/current'
    ]
    if plans_required:
        required_files.append(project_folder / 'crisp-storage/tags/plans')
    for required_file in required_files:
        if not required_file.is_file():
            print(f"WARNING: Required file '{required_file}' not found.")
            res = False
    return res


def get_workflow_for_project(project_folder: Path) -> Workflow:
    cfg = Config.from_toml_file(
        str(project_folder / 'crisp.toml'),
        mvir_storage_dir = str(project_folder / 'crisp-storage')
    )
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    workflow = Workflow(cfg, mvir)
    return workflow


def get_expected_formatted_blocks_from_seed_candidate(seed_candidate: dict[str, str]) -> dict[str, set[str]]:
    """
    Get the expected formatted blocks from all prompts in a seed candidate.

    Example:
        ```
        seed_candidate = {
            'prompt1': "hello how is {name}",
            'prompt2': "here's the number: {number}. Now, follow {instructions} as per your ID: {id}."
        }
        ```
        In this case, return value will be:
        ```
        {
            'prompt1': {'{name}'},
            'prompt2': {'{number}', '{instructions}', '{id}'}
        }
        ```
    """
    expected_formatted_blocks = {}
    for prompt_type, prompt in seed_candidate.items():
        expected_formatted_blocks[prompt_type] = set(re.findall(r'\{[^{}]+\}', prompt))
    return expected_formatted_blocks


def get_bad_prompts_in_candidate(
    candidate: dict[str, str],
    expected_formatted_blocks: dict[str, set[str]]
) -> set[str]:
    """
    Check if all prompts in a candidate have the correct formatted blocks.

    Inputs:
    - candidate: The candidate to be evaluated. E.g.:
    - expected_formatted_blocks: The expected formatted blocks in each prompt. Keys must match those in `candidate`. E.g.:

    Example:
        ```
        candidate = {
            'prompt1': "hello how is {name}",
            'prompt2': "here's the number: {number}. Now, follow {instructions}."
        }
        expected_formatted_blocks = {
            'prompt1': {'{name}'},
            'prompt2': {'{number}', '{id}', '{instructions}'}
        }
        ```
        In this case, 'prompt2' doesn't have the expected formatted blocks. Thus, {'prompt2'} will be returned.
    """
    assert candidate.keys() == expected_formatted_blocks.keys()
    assert all(
        (block.startswith('{') and block.endswith('}'))
        for expected_blocks in expected_formatted_blocks.values()
        for block in expected_blocks
    )

    bad_prompt_types = set()
    for prompt_type, current_expected_formatted_blocks in expected_formatted_blocks.items():
        if set(re.findall(r'\{[^{}]+\}', candidate[prompt_type])) != current_expected_formatted_blocks:
            bad_prompt_types.add(prompt_type)
    return bad_prompt_types
