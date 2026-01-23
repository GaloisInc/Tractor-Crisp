import os
from ..mvir import MVIR, FileNode, TreeNode
from .abc import LLMFileFormat

DEFAULT_FILE_TYPE_MAP = {
    '.rs': 'Rust',
    '.c': 'C',
    '.h': 'C',
}

class MarkdownFileFormat(LLMFileFormat):
    def __init__(self, file_type_map: dict[str, str] = DEFAULT_FILE_TYPE_MAP):
        self.file_type_map = file_type_map

    def get_output_instructions(self) -> str:
        return ('Output the updated Rust code in a Markdown code block, '
            'with the file path on the preceding line, as shown in the input.')

    def emit_file(self, n: FileNode, path: str) -> str:
        """
        Generate markdown-formatted text giving the contents of file `n`.  Produces
        output of the form:

            /path/to/file.rs
            ```Rust
            // File contents...
            ```
        """
        file_type = self.file_type_map[os.path.splitext(path)[1]]
        text = n.body_str()
        return '\n'.join((path, '```' + file_type, text, '```'))

    def extract_files(self, s: str) -> list[tuple[str, str]]:
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
