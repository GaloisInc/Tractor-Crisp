from ..mvir import MVIR, FileNode, TreeNode

from . import markdown

def _mode_to_module(mode: str):
    match mode:
        case 'markdown':
            return markdown
        case _:
            raise ValueError(f'unknown mode {mode!r}')


def get_output_instructions(mode: str, **kwargs) -> str:
    module = _mode_to_module(mode)
    return module.get_output_instructions(**kwargs)

def get_output_instructions_lowercase(mode: str, **kwargs) -> str:
    s = get_output_instructions(mode, **kwargs)
    if len(s) > 0:
        s = s[0].lower() + s[1:]
    return s

def emit_files(
    mvir: MVIR,
    mode: str,
    n: TreeNode,
    glob_filter: str = None,
    **kwargs,
) -> (str, dict[str, str]):
    module = _mode_to_module(mode)
    return module.emit_files(mvir, n, glob_filter = glob_filter, **kwargs)

def extract_files(s: str, mode: str, **kwargs) -> list[tuple[str, str]]:
    module = _mode_to_module(mode)
    return module.extract_files(s, **kwargs)
