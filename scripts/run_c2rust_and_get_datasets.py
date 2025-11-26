import csv
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from tqdm import tqdm


def run_subprocess_nodisp_check(*args, **kwargs) -> subprocess.CompletedProcess:
    """
    Run subprocess.run() with the defaults `check = True, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL`
    """
    kwargs.setdefault("check", True)
    kwargs.setdefault("stdout", subprocess.DEVNULL)
    kwargs.setdefault("stderr", subprocess.DEVNULL)
    return subprocess.run(*args, **kwargs)


class CBuilder:
    """Class to build C projects."""

    C_BUILD_STATUS_OK = 'OK'

    class CBuilderError(Exception):
        """Exceptions raised in CBuilder."""

    def __init__(self, c_project_folder: Path):
        """
        Inputs:
        - c_project_folder: Root folder of a C project.

        Other attributes:
        - c_build_folder: The build folder of a C project.
        """
        self.c_project_folder = c_project_folder
        self.c_build_folder = c_project_folder / 'build'


    def build_project(self):
        """
        Build the C project using either CMake or Make as applicable, and produce the compile_commands.json file.
        """
        self.clean_build()
        self.c_build_folder.mkdir()

        files = [f.stem.lower() for f in self.c_project_folder.glob('*')]

        if 'cmakelists' in files:
            run_subprocess_nodisp_check(
                ['cmake', '-DCMAKE_EXPORT_COMPILE_COMMANDS=1', '..'],
                cwd = self.c_build_folder,
                timeout = 10
            )

        elif 'makefile' in files:
            run_subprocess_nodisp_check(
                ['bear', '--', 'make', '-C', '..'],
                cwd = self.c_build_folder,
                timeout = 10
            )

        else:
            raise CBuilder.CBuilderError('Invalid C build system')

        if not (self.c_build_folder / 'compile_commands.json').exists():
            raise CBuilder.CBuilderError(f"Build failed, 'compile_commands.json' not created in {self.c_build_folder}/")


    def clean_build(self):
        """
        Clean the built C project.
        """
        run_subprocess_nodisp_check(
            ['make', 'clean'],
            cwd = self.c_project_folder,
            check = False
        )
        try:
            shutil.rmtree(self.c_build_folder)
        except FileNotFoundError:
            pass


class RustTranspiler:
    """Class to handle a new Rust project and populate it with C2Rust transpiled code."""

    RUST_TRANSPILE_STATUS_OK = 'OK'

    class RustTranspilerError(Exception):
        """Exceptions raised in RustTranspiler."""

    def __init__(self, rust_project_folder: Path):
        """
        Inputs:
        - rust_project_folder: Root folder of a Rust project that will be created.
        """
        self.rust_project_folder = rust_project_folder


    def create_empty_project(self):
        """
        Create the empty Rust project (overwriting existing).
        Cargo initialize it, and delete the default main.rs.
        """
        if self.rust_project_folder.is_dir():
            shutil.rmtree(self.rust_project_folder)

        if self.rust_project_folder.name[0].isdigit():
            raise RustTranspiler.RustTranspilerError(f'Rust project name cannot start with digit, but found {self.rust_project_folder.name}')

        self.rust_project_folder.mkdir()
        run_subprocess_nodisp_check(
            ['cargo', 'init'],
            cwd = self.rust_project_folder
        )
        (self.rust_project_folder / 'src/main.rs').unlink()


    def run_c2rust_transpile(self, compile_commands_json_path: Path):
        """
        Transpile C to Rust using the given `compile_commands_json_path`.
        Place transpiled artifacts in the Rust project.
        """
        run_subprocess_nodisp_check(
            ['c2rust', 'transpile', '-o', self.rust_project_folder, '--overwrite-existing', compile_commands_json_path],
            timeout = 60
        )


    def organize_rust_project(self):
        """
        After transpilation, some Rust projects contain src/src/, i.e.:
        ```
        project/
        |-- src/
            |-- src/
                |-- <src_files>
            |-- <other_files>
        ```
        Un-nest this to get:
        ```
        project/
        |-- src/
            |-- <src_files>
        |-- <other_files>
        ```
        """
        src = self.rust_project_folder / 'src'
        if (src / 'src').is_dir():
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir_path = Path(tmpdir)
                for path in src.rglob('*'):
                    path.rename(tmpdir_path / path.relative_to(src))
                shutil.rmtree(src)
                for path in tmpdir_path.rglob('*'):
                    path.rename(self.rust_project_folder / path.relative_to(tmpdir_path))


def run(c_project_folder: Path, rust_project_folder: Path) -> tuple[str, str]:
    """
    Run the complete workflow for C2Rust transpilation on an individual C project -> Rust project.

    Inputs:
    - c_project_folder: Path to the C project which will be transpiled.
    - rust_project_folder: Path to the Rust project which is created from transpilation.
        - *Not created if C building fails.*

    Returns:
    - C build status flag, containing either 'OK' or some error message.
    - Rust transpile status flag, containing either 'OK' or some error message.
    """
    c_build_status = ''
    rust_transpile_status = ''

    # Build C
    c_builder = CBuilder(c_project_folder)
    try:
        c_builder.build_project()
        c_build_status = CBuilder.C_BUILD_STATUS_OK
    except (CBuilder.CBuilderError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        c_build_status = str(e)
        return (c_build_status, rust_transpile_status)

    # Transpile to Rust
    rust_transpiler = RustTranspiler(rust_project_folder)
    try:
        rust_transpiler.create_empty_project()
        rust_transpiler.run_c2rust_transpile(compile_commands_json_path = c_builder.c_build_folder / 'compile_commands.json')
        rust_transpile_status = RustTranspiler.RUST_TRANSPILE_STATUS_OK
    except (RustTranspiler.RustTranspilerError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        rust_transpile_status = str(e)
        return (c_build_status, rust_transpile_status)

    # Clean C build
    c_builder.clean_build()

    # Organize Rust project
    rust_transpiler.organize_rust_project()

    return (c_build_status, rust_transpile_status)


def run_on_test_corpus_synthetic(test_corpus_repo_path: Path):
    """
    Run the complete workflow for C2Rust transpilation on all projects inside 'Public-Tests/B01_synthetic' in the test corpus repo.
    Create a results.csv file documenting successes and failures.
    """
    c_project_folders = sorted([f / 'test_case' for f in (test_corpus_repo_path / 'Public-Tests/B01_synthetic').iterdir() if f.is_dir()])
    rust_projects_parent_folder = Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'converted_rust_projects/c2rust_Test-Corpus_B01_synthetic'
    rust_projects_parent_folder.mkdir(parents = True, exist_ok = True)

    with open(rust_projects_parent_folder / 'results.csv', 'w', encoding='utf-8') as f:
        logger = csv.writer(f)
        logger.writerow(['c_project_folder', 'c_build_status', 'rust_project_folder', 'rust_transpile_status'])

        for c_project_folder in tqdm(c_project_folders):
            rust_project_folder = rust_projects_parent_folder / c_project_folder.parent.name[4:] # [4:] is to remove the leading 0NN_ since Rust project names cannot start with digits
            c_build_status, rust_transpile_status = run(c_project_folder = c_project_folder, rust_project_folder = rust_project_folder)
            logger.writerow([
                c_project_folder.relative_to(test_corpus_repo_path),
                c_build_status,
                rust_project_folder if c_build_status == CBuilder.C_BUILD_STATUS_OK else None,
                rust_transpile_status
            ])


def run_on_test_corpus_organic(test_corpus_repo_path: Path):
    """
    Run the complete workflow for C2Rust transpilation on all projects inside 'Public-Tests/B01_organic' in the test corpus repo.
    Create a results.csv file documenting successes and failures.
    """
    c_project_folders = sorted([f / 'test_case' for f in (test_corpus_repo_path / 'Public-Tests/B01_organic').iterdir() if f.is_dir()])
    rust_projects_parent_folder = Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'converted_rust_projects/c2rust_Test-Corpus_B01_organic'
    rust_projects_parent_folder.mkdir(parents = True, exist_ok = True)

    with open(rust_projects_parent_folder / 'results.csv', 'w', encoding='utf-8') as f:
        logger = csv.writer(f)
        logger.writerow(['c_project_folder', 'c_build_status', 'rust_project_folder', 'rust_transpile_status'])

        for c_project_folder in tqdm(c_project_folders):
            rust_project_folder = rust_projects_parent_folder / c_project_folder.parent.name
            c_build_status, rust_transpile_status = run(c_project_folder = c_project_folder, rust_project_folder = rust_project_folder)
            logger.writerow([
                c_project_folder.relative_to(test_corpus_repo_path),
                c_build_status,
                rust_project_folder if c_build_status == CBuilder.C_BUILD_STATUS_OK else None,
                rust_transpile_status
            ])


def run_on_crust_bench(crust_bench_repo_path: Path):
    """
    Run the complete workflow for C2Rust transpilation on all projects inside 'datasets/CBench' in the CRUST-Bench repo.
    Create a results.csv file documenting successes and failures.
    """
    c_project_folders = sorted([f for f in (crust_bench_repo_path / 'datasets/CBench').iterdir() if f.is_dir()])
    rust_projects_parent_folder = Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'converted_rust_projects/c2rust_CRUST-Bench'
    rust_projects_parent_folder.mkdir(parents = True, exist_ok = True)

    with open(rust_projects_parent_folder / 'results.csv', 'w', encoding='utf-8') as f:
        logger = csv.writer(f)
        logger.writerow(['c_project_folder', 'c_build_status', 'rust_project_folder', 'rust_transpile_status'])
    
        for c_project_folder in tqdm(c_project_folders):
            rust_project_folder = rust_projects_parent_folder / (
                f'proj_{c_project_folder.name}'
                if c_project_folder.name[0].isdigit()
                else c_project_folder.name
            )
            c_build_status, rust_transpile_status = run(c_project_folder = c_project_folder, rust_project_folder = rust_project_folder)
            logger.writerow([
                c_project_folder.relative_to(crust_bench_repo_path),
                c_build_status,
                rust_project_folder if c_build_status == CBuilder.C_BUILD_STATUS_OK else None,
                rust_transpile_status
            ])


if __name__ == "__main__":
    run_on_test_corpus_synthetic(Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'Test-Corpus')
    run_on_test_corpus_organic(Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'Test-Corpus')
    run_on_crust_bench(Path(os.path.dirname(os.path.realpath(__file__))).resolve().parent.parent / 'CRUST-bench')
