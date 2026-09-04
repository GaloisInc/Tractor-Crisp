"""Common code for GEPA prompt optimization."""

import dataclasses
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


def get_expected_formatted_blocks(seed_candidate: dict[str, str]) -> dict[str, set[str]]:
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
    assert all(
        (block.startswith('{') and block.endswith('}'))
        for expected_blocks in expected_formatted_blocks.values()
        for block in expected_blocks
    )
    return expected_formatted_blocks


@dataclasses.dataclass(frozen=True)
class FBlockReport:
    """Report on mismatch in formatted blocks between a candidate prompt and the expectation."""

    missing_expected_fblocks: set[str] = dataclasses.field(default_factory=set)
    """Formatted blocks which the candidate is expected to have, but doesn't."""

    has_extra_fblocks: set[str] = dataclasses.field(default_factory=set)
    """Formatted blocks which the candidate has, but were not expected."""


def analyze_formatted_blocks_in_candidate(
    candidate: dict[str, str],
    expected_formatted_blocks: dict[str, set[str]]
) -> dict[str, FBlockReport]:
    """
    Given a candidate with prompts and the expected formatted blocks for each prompt, return the FBlockReport for each prompt.

    Example:
        ```
        process_formatted_blocks_in_candidate(
            candidate = {
                'prompt1': "Hello how is {name}? Good to see {person}.",
                'prompt2': "Here's the number: {number}. Now, follow {instructions}. Remember to call {abc} and {xyz}, okay?"
            }
            expected_formatted_blocks = {
                'prompt1': {'{name}'},
                'prompt2': {'{number}', '{id}', '{instructions}'}
            }
        )
        will return
        {
            'prompt1': FBlockReport(
                missing_expected_fblocks = set(),
                has_extra_fblocks = {'{person}'}
            ),
            'prompt2': FBlockReport(
                missing_expected_fblocks = {'{id}'},
                has_extra_fblocks = {'{abc}', '{xyz}'}
            )
        }
        ```
    """
    assert candidate.keys() == expected_formatted_blocks.keys()
    result = {}
    for prompt_type, current_expected_formatted_blocks in expected_formatted_blocks.items():
        candidate_formatted_blocks = set(re.findall(r'\{[^{}]+\}', candidate[prompt_type]))
        result[prompt_type] = FBlockReport(
            missing_expected_fblocks = current_expected_formatted_blocks - candidate_formatted_blocks,
            has_extra_fblocks = candidate_formatted_blocks - current_expected_formatted_blocks
        )
    return result
