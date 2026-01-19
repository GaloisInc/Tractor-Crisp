import os
import pathlib
from ..mvir import MVIR, FileNode, TreeNode

def get_output_instructions() -> str:
    return ('Output the updated Rust code in a Markdown code block, '
        'with the file path on the preceding line, as shown in the input.')

def emit_file(n: FileNode, path, file_type='Rust'):
    """
    Generate markdown-formatted text giving the contents of file `n`.  Produces
    output of the form:

        /path/to/file.rs
        ```Rust
        // File contents...
        ```
    """
    text = n.body().decode('utf-8')
    return '\n'.join((path, '```' + file_type, text, '```'))

DEFAULT_FILE_TYPE_MAP = {
    '.rs': 'Rust',
    '.c': 'C',
    '.h': 'C',
}

def emit_files(
    mvir: MVIR,
    n: TreeNode,
    glob_filter: str = None,
    file_type_map: dict[str, str] = DEFAULT_FILE_TYPE_MAP,
) -> (str, dict[str, str]):
    """
    Generate markdown-formatted text giving the contents of files in `n`, along
    with a dict mapping short path names used in the output to full paths as
    used in `n`.  Output is formatted like `emit_file`.  If `glob_filter` is
    set to a string, only files whose paths match that glob pattern will be
    included.
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

        file_type = file_type_map[os.path.splitext(path)[1]]
        child_node = mvir.node(child_id)
        parts.append(emit_file(child_node, short_path, file_type=file_type))
    return '\n\n'.join(parts), short_path_map

def extract_files(s):
    """
    Extract from `s` all markdown code blocks that appear to match the format
    of `emit_file`.
    """
    files = []

    lines = s.splitlines()
    # `start_i` is the index of the opening line of a markdown code block
    # ("```Rust" or similar), or `None` if we aren't currently in a block.
    start_i = None
    for i, line in enumerate(lines):
        if line == '```':
            if start_i is not None:
                path = lines[start_i - 1]
                text = '\n'.join(lines[start_i + 1 : i]) + '\n'
                files.append((path, text))
            start_i = None
        elif line.startswith('```'):
            start_i = None
            if i == 0:
                continue

            # The line before the start of the block should contain the file
            # path.
            path = lines[i - 1]
            # Some heuristics to reject non-path text before the block.
            if len(path.split(None, 1)) > 1:
                # Invalid path (contains whitespace)
                continue
            if '..' in path:
                continue
            if os.path.normpath(path) != path:
                continue

            file_type = DEFAULT_FILE_TYPE_MAP[os.path.splitext(path)[1]]
            if line.strip().lower() != '```' + file_type.lower():
                continue

            start_i = i

    return files
