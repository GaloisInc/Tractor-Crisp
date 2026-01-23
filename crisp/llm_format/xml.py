import os
import pathlib
import re
from ..mvir import MVIR, FileNode, TreeNode
from .abc import LLMFileFormat

RE_OPEN_TAG = re.compile(r'^<file name="([^"]*)">$')

class XmlFileFormat(LLMFileFormat):
    def get_output_instructions(self) -> str:
        return ('Output the code for each modified file inside '
            '<file name="foo.rs">...</file>, as shown in the input.')

    def emit_file(self, n: FileNode, path: str) -> str:
        """
        Generate markdown-formatted text giving the contents of file `n`.  Produces
        output of the form:

            <file name="/path/to/file.rs">
            // File contents...
            </file>
        """
        return '\n'.join((f'<file name="{path}">', n.body_str(), '</file>'))

    def extract_files(self, s: str) -> list[tuple[str, str]]:
        """
        Extract from `s` all XML code blocks that appear to match the format of
        `emit_file`.
        """
        files = []

        lines = s.splitlines()
        # `start_i` is the index of the opening line of an XML block (similar to
        # `<file name="foo.rs">`), or `None` if we aren't currently in a block.
        start_i = None
        start_path = None
        for i, line in enumerate(lines):
            if (m := RE_OPEN_TAG.match(line)):
                start_i, start_path = None, None

                path = m.group(1)
                # Some heuristics to reject bad paths.
                if len(path.split(None, 1)) > 1:
                    # Invalid path (contains whitespace)
                    continue
                if '..' in path:
                    continue
                if os.path.normpath(path) != path:
                    continue

                start_i = i
                start_path = path

            elif line == '</file>':
                if start_i is not None:
                    path = start_path
                    text = '\n'.join(lines[start_i + 1 : i]) + '\n'
                    files.append((path, text))
                start_i, start_path = None, None

        return files
