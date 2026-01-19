import os
import pathlib

from ..mvir import MVIR, FileNode, TreeNode

from . import markdown
from . import xml

def _mode_to_module(mode: str):
    match mode:
        case 'markdown':
            return markdown
        case 'xml':
            return xml
        case _:
            raise ValueError(f'unknown mode {mode!r}')


def get_output_instructions(mode: str, **kwargs) -> str:
    '''
    Returns output formatting instructions for inclusion in the LLM prompt.
    This will be something like:

        Output the new code in foo format with bar delimiters.  Always do X;
        never do Y.
    '''
    module = _mode_to_module(mode)
    return module.get_output_instructions(**kwargs)

def get_output_instructions_lowercase(mode: str, **kwargs) -> str:
    '''
    Like `get_output_instructions`, but the first letter is lowercased, so it
    can be prefixed with an introductory clause.  Example usage:

        'After finishing the task, ' + get_output_instructions_lowercase(mode)

    This produces something like:

        After finishing the task, output the new code in foo format with bar
        delimiters.  Always do X; never do Y.
    '''

    s = get_output_instructions(mode, **kwargs)
    if len(s) > 0:
        s = s[0].lower() + s[1:]
    return s

def emit_file(
    mode: str,
    n: FileNode,
    path: str,
    **kwargs,
) -> (str, dict[str, str]):
    module = _mode_to_module(mode)
    return module.emit_file(n, path, **kwargs)

def emit_files(
    mvir: MVIR,
    mode: str,
    n: TreeNode,
    glob_filter: str = None,
    **kwargs,
) -> (str, dict[str, str]):
    """
    Generate formatted text giving the contents of files in `n`, along with a
    dict mapping short path names used in the output to full paths as used in
    `n`.  Output is formatted like `emit_file`.  If `glob_filter` is set to a
    string, only files whose paths match that glob pattern will be included.
    """
    assert isinstance(n, TreeNode)

    if isinstance(glob_filter, str):
        glob_filter = (glob_filter,)

    if len(n.files) == 0:
        common_prefix = ''
    elif len(n.files) == 1:
        common_prefix = os.path.dirname(list(n.files.keys())[0])
    else:
        common_prefix = os.path.commonpath(n.files.keys())

    parts = []
    short_path_map = {}
    for path, child_id in n.files.items():
        if glob_filter is not None:
            path_obj = pathlib.Path(path)
            glob_match = any(path_obj.match(g) for g in glob_filter)
            if not glob_match:
                continue

        short_path = os.path.relpath(path, common_prefix)
        assert short_path not in short_path_map
        short_path_map[short_path] = path

        child_node = mvir.node(child_id)
        part = emit_file(mode, child_node, short_path, **kwargs)
        parts.append(part)
    return '\n\n'.join(parts), short_path_map

def extract_files(s: str, mode: str, **kwargs) -> list[tuple[str, str]]:
    module = _mode_to_module(mode)
    return module.extract_files(s, **kwargs)
