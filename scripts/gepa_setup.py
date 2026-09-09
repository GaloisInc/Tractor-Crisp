"""This script sets up any dataset or project for the first time, after which GEPA can be run.
```
python scripts/gepa_setup.py <path>
```

=================== ARGUMENTS ===================
`path` is EITHER:
- A Test-Corpus dataset directory containing subfolders for projects. Must be inside `Test-Corpus/Public-Tests/` inside this repo. Values can be:
    - B01_organic
    - B01_synthetic
    - B02_organic
    - B01_synthetic
    - More coming soon ...
OR
- An individual project directory. Must be a sibling to this repo. Values can be:
    - zlib
    - More coming soon ...

=================== BEHAVIOR ===================
This script does the following for <all projects inside `path` if `path` is a dataset directory> / <the project at `path` if `path` is an individual project directory>:
1. Use C2Rust to convert C code to unsafe Rust. Make sure it compiles and passes tests. When done, the 'crisp-storage/tags/current' node of all the dataset's projects will point to the unsafe Rust. *This is usually a good starting point for GEPA workflows.*
2. Use an AI agent to create a safety plan for refactoring the unsafe Rust. When done, the 'plans' node of all the dataset's projects will point to this plan.

============= RECOMMENDED FOLLOWUP ==============
After this script finishes, it is recommended to backup the `crisp-storage/` directory somewhere.
E.g. `cp -r ../zlib/crisp-storage ../Tractor_backups/zlib_crisp_storage_backup`
"""

import argparse
import os
from pathlib import Path
import subprocess
from tqdm import tqdm


def dataset_setup_initial(dataset_dir: Path):
    project_dirs = [p for p in dataset_dir.iterdir() if p.is_dir()]
    for project_dir in tqdm(project_dirs):
        subprocess.run(
            ["python", "scripts/test_eval.py", project_dir],
            env = {**os.environ, "LLM_SAFETY_TRIES": "0"},
            cwd = str(Path(__file__).resolve().parent.parent), # run from repo root
        )
        subprocess.run(
            ["python", "scripts/save_plans.py", project_dir],
            cwd = str(Path(__file__).resolve().parent.parent), # run from repo root
        )


def project_setup_initial(project_dir: Path):
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


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
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
        dataset_setup_initial(dataset_dir=dataset_dir)

    # Individual project
    else:
        project_dir = Path(__file__).resolve().parent.parent.parent / args.dir
        assert project_dir.is_dir(), f"Project directory {project_dir} doesn't exist."
        project_setup_initial(project_dir=project_dir)


if __name__ == "__main__":
    args = parse_args()
    main(args)
