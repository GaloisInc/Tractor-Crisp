"""Run this script prior to running GEPA on any dataset or project.
```
python scripts/gepa_setup.py <dataset_dir/project_dir> <initial/rerun>
```


==================== ARGUMENTS ====================

1st argument is EITHER:
- A Test-Corpus directory containing projects, must be inside `Test-Corpus/Public-Tests/` inside this repo. Values can be:
    - B01_organic
    - B01_synthetic
    - B02_organic
    - B01_synthetic
OR
- An individual project directory, must be a sibling to this repo. Values can be:
    - zlib

2nd argument is either "initial" to run initial setup before running GEPA for the first time,
or "rerun" to get things back into place for a fresh GEPA run.


==================== BEHAVIOR ====================

When running `python scripts/gepa_setup.py <dataset_dir> <initial>`, this does the following on all projects inside the dataset:
1. Use C2Rust to convert C code to unsafe Rust. Make sure it compiles and passes tests. When done, the 'current' node of all the dataset's projects will point to the unsafe Rust.
2. Use an AI agent to create a safety plan for refactoring the unsafe Rust. When done, the 'plans' node of all the dataset's projects will point to this plan.
3. Place an entire backup of the dataset directory in the parent location of this repo (e.g. at `../B01_organic_gepaready_backup/`). This can be used for re-running.

When running `python scripts/gepa_setup.py <project_dir> <initial>`, this does steps 1 and 2 of the above workflow on the individual project. For step 3, instead of backing up the entire directory, it only backs up the `crisp-storage/` directory (e.g. `../zlib/crisp-storage/` is backed up to `../zlib_crisp_storage_gepaready_backup/`). This is because the entire directory is assumed to be a Git repo, so we don't want to back it up in its entirety.

When running `python scripts/gepa_setup.py <dataset_dir> <rerun>`, copies the backup to the main location (e.g., copies `../B01_organic_gepaready_backup/` to `Test-Corpus/Public-Tests/B01_organic/`).

When running `python scripts/gepa_setup.py <project_dir> <rerun>`, copies the crisp-storage backup to the main location (e.g., copies `../zlib_crisp_storage_gepaready_backup/` to `../zlib/crisp-storage/`).
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
from tqdm import tqdm


def dataset_setup_initial(dataset_dir: Path, initial_setup_backup_path: Path):
    projects = [p for p in dataset_dir.iterdir() if p.is_dir()]
    for project in tqdm(projects):
        subprocess.run(
            ["python", "scripts/test_eval.py", project],
            env = {**os.environ, "LLM_SAFETY_TRIES": "0"},
            cwd = str(Path(__file__).resolve().parent.parent), # run from repo root
        )
        subprocess.run(
            ["python", "scripts/save_plans.py", project],
            cwd = str(Path(__file__).resolve().parent.parent), # run from repo root
        )
    shutil.copytree(dataset_dir, initial_setup_backup_path)


def dataset_setup_rerun(dataset_dir: Path, initial_setup_backup_path: Path):
    shutil.rmtree(dataset_dir)
    shutil.copytree(initial_setup_backup_path, dataset_dir)


def project_setup_initial(project_dir: Path, initial_setup_crisp_storage_backup_path: Path):
    subprocess.run(
        ["crisp", "commit", "-t", "c_code", "."],
        cwd = str(project_dir),
    )
    subprocess.run(
        ["crisp", "main"],
        env = {**os.environ, "LLM_SAFETY_TRIES": "0"},
        cwd = str(project_dir),
    )
    subprocess.run(
        ["python", "scripts/save_plans.py", str(project_dir)],
        cwd = str(Path(__file__).resolve().parent.parent), # run from repo root
    )
    shutil.copytree(project_dir / 'crisp-storage', initial_setup_crisp_storage_backup_path)


def project_setup_rerun(project_dir: Path, initial_setup_crisp_storage_backup_path: Path):
    shutil.rmtree(project_dir / 'crisp-storage')
    shutil.copytree(initial_setup_crisp_storage_backup_path, project_dir / 'crisp-storage')


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument(
        "setup",
        choices = ["initial", "rerun"]
    )
    return ap.parse_args()


def main(args: argparse.Namespace):

    # Dataset
    if args.dir in [
        "B01_organic",
        "B01_synthetic",
        "B02_organic",
        "B01_synthetic",
    ]:
        dataset_dir = Path(__file__).resolve().parent.parent / f"Test-Corpus/Public-Tests/{args.dir}"
        assert dataset_dir.is_dir(), f"Dataset directory {dataset_dir} doesn't exist."

        initial_setup_backup_path = Path(__file__).resolve().parent.parent.parent / f"{args.dir}_gepaready_backup"

        if args.setup == "initial":
            assert not initial_setup_backup_path.is_dir(), f"Backup path {initial_setup_backup_path} already exists. Cannot do initial setup. Aborting."
            dataset_setup_initial(
                dataset_dir = dataset_dir,
                initial_setup_backup_path = initial_setup_backup_path
            )

        else:
            assert initial_setup_backup_path.is_dir(), f"Backup path {initial_setup_backup_path} doesn't exist. Cannot do rerun setup. Aborting."
            dataset_setup_rerun(
                dataset_dir = dataset_dir,
                initial_setup_backup_path = initial_setup_backup_path
            )

    # Individual project
    else:
        project_dir = Path(__file__).resolve().parent.parent.parent / args.dir
        assert project_dir.is_dir(), f"Project directory {project_dir} doesn't exist."

        initial_setup_crisp_storage_backup_path = Path(__file__).resolve().parent.parent.parent / f"{args.dir}_crisp_storage_gepaready_backup"

        if args.setup == "initial":
            assert not initial_setup_crisp_storage_backup_path.is_dir(), f"Backup path {initial_setup_crisp_storage_backup_path} already exists. Cannot do initial setup. Aborting."
            project_setup_initial(
                project_dir = project_dir,
                initial_setup_crisp_storage_backup_path = initial_setup_crisp_storage_backup_path
            )

        else:
            assert initial_setup_crisp_storage_backup_path.is_dir(), f"Backup path {initial_setup_crisp_storage_backup_path} doesn't exist. Cannot do rerun setup. Aborting."
            project_setup_rerun(
                project_dir = project_dir,
                initial_setup_crisp_storage_backup_path = initial_setup_crisp_storage_backup_path
            )


if __name__ == "__main__":
    args = parse_args()
    main(args)
