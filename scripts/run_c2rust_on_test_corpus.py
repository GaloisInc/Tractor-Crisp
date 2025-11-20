import os
from pathlib import Path
import shutil
import subprocess
from tqdm import tqdm


TEST_CORPUS_PATH = Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'Test-Corpus'
CONVERTED_RUST_PATH = Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'Test-Corpus-Converted-Rust'

project_folder_paths = []
project_folder_paths.extend(sorted([f for f in (TEST_CORPUS_PATH / 'Public-Tests/B01_synthetic').iterdir() if f.is_dir()]))
project_folder_paths.extend(sorted([f for f in (TEST_CORPUS_PATH / 'Public-Tests/B01_organic').iterdir() if f.is_dir()]))

for project_folder_path in tqdm(project_folder_paths):

    # Build C project
    c_project_folder_path = project_folder_path / 'test_case'
    c_build_path = c_project_folder_path / 'build'
    c_build_path.mkdir()
    subprocess.run(
        ['cmake', '-DCMAKE_EXPORT_COMPILE_COMMANDS=1', '..'],
        cwd = c_build_path
    )

    # Run C2Rust
    try:
        subprocess.run(
            ['c2rust', 'transpile', c_build_path / 'compile_commands.json'],
            check = True,
            capture_output = True
        )
    except subprocess.CalledProcessError as e:
        with open('run_c2rust_on_test_corpus_failure_report.txt', 'a', encoding='utf-8') as f:
            f.write(f'==========\nC2Rust failed on {project_folder_path.relative_to(TEST_CORPUS_PATH)}\n==========\n\nOutput: {e.output}\n\nError: {e.stderr}\n\n')
    c2rust_output_file_paths = [f for f in (c_project_folder_path / 'src').iterdir() if f.suffix == '.rs']

    # Create Rust project
    rust_project_folder_name = f'c2rust_{project_folder_path.stem}'
    rust_project_folder_parent = CONVERTED_RUST_PATH / project_folder_path.relative_to(TEST_CORPUS_PATH)
    rust_project_folder_parent.mkdir()
    subprocess.run(
        ['cargo', 'new', rust_project_folder_name],
        cwd = rust_project_folder_parent
    )
    rust_project_folder_path = rust_project_folder_parent / rust_project_folder_name
    (rust_project_folder_path / 'src/main.rs').unlink()

    # Move C2Rust output files to Rust project
    for c2rust_output_file_path in c2rust_output_file_paths:
        c2rust_output_file_path.rename(rust_project_folder_path / 'src' / c2rust_output_file_path.name)

    # Clear C build
    shutil.rmtree(c_build_path)
