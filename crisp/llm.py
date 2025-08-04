import json
import os
import pathlib
import requests

from .mvir import MVIR, FileNode, TreeNode, LlmOpNode


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

    if isinstance(glob_filter, str):
        glob_filter = (glob_filter,)

    parts = []
    for path, child_id in n.files.items():
        if glob_filter is not None:
            path_obj = pathlib.Path(path)
            glob_match = any(path_obj.match(g) for g in glob_filter)
            if not glob_match:
                continue

        file_type = file_type_map[os.path.splitext(path)[1]]
        child_node = mvir.node(child_id)
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


LLM_ENDPOINT = 'http://localhost:8080/v1/chat/completions'

def run_rewrite(
        mvir: MVIR,
        prompt_fmt: str,
        input_code: TreeNode,
        *,
        glob_filter: str | list[str] | None = None,
        file_type_map = DEFAULT_FILE_TYPE_MAP,
        format_kwargs: dict = {},
        think: bool = False,
        ) -> TreeNode:
    input_files_str = emit_files(mvir, input_code, glob_filter=glob_filter,
        file_type_map=file_type_map)
    prompt = prompt_fmt.format(input_files=input_files_str, **format_kwargs)
    prompt_without_files = prompt_fmt.format(input_files='{input_files}', **format_kwargs)

    req_messages = [
        {'role': 'user', 'content': prompt},
    ]
    if not think:
        req_messages.append({'role': 'assistant', 'content': '<think>\n</think>\n'})
    req = {'messages': req_messages}
    resp = requests.post(LLM_ENDPOINT, json=req).json()

    output = resp['choices'][0]['message']['content']
    print(' === output ===')
    print(output)
    print(' === end of output ===')

    output_files = input_code.files.copy()
    for out_path, out_text in extract_files(output):
        assert out_path in output_files, \
            'output contained unknown file path %r' % (out_path,)
        # TODO: also check that `out_path` matches `glob_filter`
        output_files[out_path] = FileNode.new(mvir, out_text.encode('utf-8')).node_id()
    output_code = TreeNode.new(mvir, files=output_files)

    n_op = LlmOpNode.new(
            mvir,
            old_code = input_code.node_id(),
            new_code = output_code.node_id(),
            raw_prompt = FileNode.new(mvir, prompt_without_files).node_id(),
            request = FileNode.new(mvir, json.dumps(req)).node_id(),
            response = FileNode.new(mvir, json.dumps(resp)).node_id(),
            )
    # Record operations and timestamps in the `op_history` reflog.
    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)

    return output_code, n_op
