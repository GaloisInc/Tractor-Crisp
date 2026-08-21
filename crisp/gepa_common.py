"""Common code for GEPA prompt optimization."""

from pathlib import Path


# Don't mess with these because GEPA internals depend on these
GEPA_MIN_SCORE = 0.
GEPA_MAX_SCORE = 1.


def is_project_gepaready(project_folder: Path) -> bool:
    """
    Given a project folder, check if it has the required files to run GEPA and return True / False accordingly.
    """
    res = True
    for required_file in [
        project_folder / 'crisp.toml',
        project_folder / 'crisp-storage/tags/c_code',
        project_folder / 'crisp-storage/tags/current'
    ]:
        if not required_file.is_file():
            print(f"WARNING: Required file '{required_file}' not found.")
            res = False
    return res
