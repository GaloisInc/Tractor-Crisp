import os
from pathlib import Path
import shutil
import subprocess
from tqdm import tqdm


def run_c2rust(
    c_project_folder: Path,
    rust_project_folder: Path
):

    # Build C project
    c_build_folder = c_project_folder / 'build'
    c_build_folder.mkdir()
    subprocess.run(
        ['cmake', '-DCMAKE_EXPORT_COMPILE_COMMANDS=1', '..'],
        cwd = c_build_folder
    )

    # Create Rust project
    rust_project_folder.mkdir(parents = True)
    subprocess.run(
        ['cargo', 'init'],
        cwd = rust_project_folder
    )
    (rust_project_folder / 'src/main.rs').unlink()

    # Run C2Rust
    subprocess.run(
        ['c2rust', 'transpile', '-o', rust_project_folder, '--overwrite-existing', c_build_folder / 'compile_commands.json'],
        check = True,
        capture_output = True
    )

    # Clear C build
    shutil.rmtree(c_build_folder)


def run_c2rust_on_test_corpus():
    test_corpus_repo = Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'Test-Corpus'

    # Synthetic
    c_project_folders = sorted([f / 'test_case' for f in (test_corpus_repo / 'Public-Tests/B01_synthetic').iterdir() if f.is_dir()])
    for c_project_folder in tqdm(c_project_folders):
        run_c2rust(
            c_project_folder = c_project_folder,
            rust_project_folder = Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'converted_rust_projects/c2rust_Test-Corpus_B01_synthetic' / c_project_folder.parent.name[4:] # [4:] is to remove the leading 0NN_ since Rust project names cannot start with digits
        )

    # Organic
    c_project_folders = sorted([f / 'test_case' for f in (test_corpus_repo / 'Public-Tests/B01_organic').iterdir() if f.is_dir()])
    for c_project_folder in tqdm(c_project_folders):
        run_c2rust(
            c_project_folder = c_project_folder,
            rust_project_folder = Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'converted_rust_projects/c2rust_Test-Corpus_B01_organic' / c_project_folder.parent.name
        )

def run_c2rust_on_crust_bench():
    crust_bench_repo = Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'CRUST-bench'
    c_project_folders = sorted([f for f in (crust_bench_repo / 'datasets/CBench').iterdir() if f.is_dir()])
    for c_project_folder in tqdm(c_project_folders):
        run_c2rust(
            c_project_folder = c_project_folder,
            rust_project_folder = Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'converted_rust_projects/c2rust_CRUST-Bench' / c_project_folder.name
        )


if __name__ == "__main__":
    run_c2rust_on_test_corpus()
    run_c2rust_on_crust_bench()
