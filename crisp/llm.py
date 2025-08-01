import os
import pathlib

from .mvir import MVIR, FileNode, TreeNode


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

def emit_files(mvir: MVIR, n: TreeNode, glob_filter=None, file_type_map=DEFAULT_FILE_TYPE_MAP):
    """
    Generate markdown-formatted text giving the contents of files in `n`.
    Output is formatted like `emit_file`.  If `glob_filter` is set to a string,
    only files whose paths match that glob pattern will be included.
    """
    assert isinstance(n, TreeNode)
    parts = []
    for path, child_id in n.files:
        if glob_filter is not None:
            glob_match = pathlib.Path(path).match(glob_filter)
            if not glob_match:
                continue

        file_type = file_type_map[os.path.splitext(path)[1]]
        child_node = mvir.get(child_id)
        parts.append(emit_file(child_node, path, file_type=file_type))
    return '\n\n'.join(parts)

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

