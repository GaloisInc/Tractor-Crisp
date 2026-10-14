from pathlib import Path
import re


_PROMPT_DIR = Path(__file__).parent / 'prompts'
def _prompt(name: str) -> str:
    """
    Load a prompt from `crisp/prompts/{name}`.
    """
    return (_PROMPT_DIR / name).read_text()

AGENT_PLAN = _prompt('agent_plan.md')

AGENT_SAFETY_REVIEW = _prompt('safety_review.md')

SAFETY_REVIEW_RULES = _prompt('safety_review_rules.md').strip()

FFI_ENTRY_POINT_RULES = _prompt('ffi_entry_point_rules.md').strip()

# `codex exec review` renders each finding as `- [P1] title — file:line`;
# the prompt places an explicit verdict in its rendered overall explanation.
AGENT_FFI_REVIEW_FINDING_RE = re.compile(r'^\s*-\s*\[P\d+\]', re.MULTILINE)
