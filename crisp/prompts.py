from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader

_PROMPT_DIR = Path(__file__).parent / 'prompts'

def _prompt(name: str) -> str:
    """
    Load a prompt from `crisp/prompts/{name}`.
    """
    return (_PROMPT_DIR / name).read_text()


jinja_env = Environment(
    loader = FileSystemLoader(str(_PROMPT_DIR)),
    autoescape = False,
    keep_trailing_newline = True,
    trim_blocks = True,
    lstrip_blocks = True,
)


FFI_ENTRY_POINT_RULES = _prompt('ffi_entry_point_rules.md').strip()

# `codex exec review` renders each finding as `- [P1] title — file:line`;
# a clean review is prose with no such lines.
AGENT_FFI_REVIEW_FINDING_RE = re.compile(r'^\s*-\s*\[P\d+\]', re.MULTILINE)


def llm_safety_no_ffi(
    output_instructions: str,
    output_instructions_lowercase: str,
    input_files: str,
) -> str:
    """Render the llm safety no ffi prompt."""
    return llm_safety(
        output_instructions = output_instructions,
        output_instructions_lowercase = output_instructions_lowercase,
        input_files = input_files,
        has_ffi = False,
    )

def llm_safety(
    output_instructions: str,
    output_instructions_lowercase: str,
    input_files: str,
    has_ffi: bool = True,
) -> str:
    """Render the llm safety prompt."""
    return jinja_env.get_template('llm_safety.jinja').render(
        output_instructions_lowercase = output_instructions_lowercase,
        input_files = input_files,
        has_ffi = has_ffi,
    )


def llm_repair_tests(
    output_instructions: str,
    output_instructions_lowercase: str,
    input_files: str,
    test_output: str,
) -> str:
    """Render the llm repair tests prompt."""
    return jinja_env.get_template('llm_repair_tests.jinja').render(
        output_instructions = output_instructions,
        input_files = input_files,
        test_output = test_output,
    )

def llm_repair_compile(
    output_instructions: str,
    output_instructions_lowercase: str,
    input_files: str,
    stderr: str,
) -> str:
    """Render the llm repair compile prompt."""
    return jinja_env.get_template('llm_repair_compile.jinja').render(
        output_instructions = output_instructions,
        input_files = input_files,
        stderr = stderr,
    )

def llm_repair_safety(
    output_instructions: str,
    output_instructions_lowercase: str,
    input_files: str,
    logs: str,
) -> str:
    """Render the llm repair safety prompt."""
    return jinja_env.get_template('llm_repair_safety.jinja').render(
        output_instructions = output_instructions,
        input_files = input_files,
        logs = logs,
    )

def llm_repair_call_sites(
    output_instructions: str,
    output_instructions_lowercase: str,
    old_sigs: str,
    new_sigs: str,
    input_files: str,
) -> str:
    """Render the llm repair call sites prompt."""
    return jinja_env.get_template('llm_repair_call_sites.jinja').render(
        output_instructions = output_instructions,
        old_sigs = old_sigs,
        new_sigs = new_sigs,
        input_files = input_files,
    )


def agent_safety(
    target_goal: str,
    after_refactoring_instruction: str,
) -> str:
    """Render the agent safety prompt."""
    return jinja_env.get_template('agent_safety.jinja').render(
        target_goal = target_goal,
        after_refactoring_instruction = after_refactoring_instruction,
    )


def agent_ffi_rejected(
    report: str,
) -> str:
    """Render the agent ffi rejected prompt."""
    return jinja_env.get_template('agent_ffi_rejected.jinja').render(
        report = report,
    )

def agent_ffi_seen_findings(
    findings: str,
) -> str:
    """Render the agent ffi seen findings prompt."""
    return jinja_env.get_template('agent_ffi_seen_findings.jinja').render(
        findings = findings,
    )


def agent_after_refactoring_run_tests() -> str:
    """Render the agent after refactoring run tests prompt."""
    return jinja_env.get_template('agent_after_refactoring_run_tests.jinja').render(
    )

def agent_after_refactoring_build() -> str:
    """Render the agent after refactoring build prompt."""
    return jinja_env.get_template('agent_after_refactoring_build.jinja').render(
    )


def agent_target_field(
    field_name: str,
    struct_name: str,
) -> str:
    """Render the agent target field prompt."""
    return jinja_env.get_template('agent_target_field.jinja').render(
        field_name = field_name,
        struct_name = struct_name,
    )

def agent_target_function(
    func_name: str,
) -> str:
    """Render the agent target function prompt."""
    return jinja_env.get_template('agent_target_function.jinja').render(
        func_name = func_name,
    )


def agent_plan(cargo_dir_path: str) -> str:
    """Render the agent plan prompt with the FFI entry point rules."""
    return jinja_env.get_template('agent_plan.jinja').render(
        cargo_dir_path = cargo_dir_path,
        ffi_entry_point_rules = FFI_ENTRY_POINT_RULES,
    )


def ffi_review(cargo_dir_path: str) -> str:
    """Render the ffi review prompt with the FFI entry point rules."""
    return jinja_env.get_template('ffi_review.jinja').render(
        cargo_dir_path = cargo_dir_path,
        ffi_entry_point_rules = FFI_ENTRY_POINT_RULES,
    )
