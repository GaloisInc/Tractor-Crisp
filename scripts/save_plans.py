import argparse
from pathlib import Path

from crisp.gepa_po import is_project_gepaready
from crisp.config import Config
from crisp.mvir import MVIR
from crisp.workflow import Workflow
from crisp.__main__ import parse_node_id_arg


def process_project(project_folder: Path, overwrite: bool):
    """
    Process a `project_folder` to get its plans and save them as a node with the `plans` tag.
    If `plans` already exist and `overwrite = False`, nothing will be done.
    If `project_folder` doesn't have the required files, nothing will be done.
    """

    if is_project_gepaready(project_folder):
        cfg = Config.from_toml_file(
            str(project_folder / 'crisp.toml'),
            mvir_storage_dir = str(project_folder / 'crisp-storage')
        )
        mvir = MVIR(cfg.mvir_storage_dir, '.')
        workflow = Workflow(cfg, mvir)

        exists = False
        try:
            mvir.node(parse_node_id_arg(mvir, 'plans'))
            exists = True
        except ValueError:
            pass

        if exists and not overwrite:
            print(f"Skipping project '{project_folder.name}' because its plans already exist and `overwrite = False`.")
        else:
            print(f"Processing project '{project_folder.name}' ...")
            n_plans = workflow.do_safety_plan_agent(
                n_code = mvir.node(parse_node_id_arg(mvir, 'current')), #NOTE: This assumes that 'current' is the node corresponding to the non-rewritten, unsafe C2Rust output. See the docstring of `gepa_setup_initial.sh` for more details.
                n_test_code =  mvir.node(parse_node_id_arg(mvir, 'c_code'))
            )[1]
            mvir.set_tag('plans', n_plans.node_id())
            print(f"Done processing project '{project_folder.name}.'")

    else:
        print(f"WARNING: Skipping project '{project_folder.name}' because required file(s) were not found.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "project_folder",
        type = Path,
        help = "The project folder to run on"
    )
    parser.add_argument(
        '-o', "--overwrite",
        action = 'store_true',
        help = "Whether to overwrite existing plans or not"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    process_project(project_folder = args.project_folder, overwrite = args.overwrite)
