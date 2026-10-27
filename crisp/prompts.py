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


AGENT_PLAN = _prompt('agent_plan.md')

AGENT_FFI_REVIEW = _prompt('ffi_review.md')

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
