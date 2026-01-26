from abc import ABCMeta, abstractmethod
import os
import pathlib

from ..mvir import MVIR, FileNode, TreeNode

class LLMFileFormat(metaclass = ABCMeta):
    @abstractmethod
    def get_output_instructions(self) -> str:
        '''
        Returns output formatting instructions for inclusion in the LLM prompt.
        This will be something like:

            Output the new code in foo format with bar delimiters.  Always do X;
            never do Y.
        '''
        ...

    def get_output_instructions_lowercase(self) -> str:
        '''
        Like `get_output_instructions`, but the first letter is lowercased, so
        it can be prefixed with an introductory clause.  Example usage:

            'After finishing the task, ' + formatter.get_output_instructions_lowercase()

        This produces something like:

            After finishing the task, output the new code in foo format with
            bar delimiters.  Always do X; never do Y.
        '''

        s = self.get_output_instructions()
        if len(s) > 0:
            s = s[0].lower() + s[1:]
        return s

    @abstractmethod
    def emit_file(self, n: FileNode, path: str) -> str:
        ...

    def emit_files(
        self,
        mvir: MVIR,
        n: TreeNode,
        glob_filter: str = None,
    ) -> (str, dict[str, str]):
        """
        Generate formatted text giving the contents of files in `n`, along with
        a dict mapping short path names used in the output to full paths as
        used in `n`.  Output is formatted like `emit_file`.  If `glob_filter`
        is set to a string, only files whose paths match that glob pattern will
        be included.
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
            part = self.emit_file(child_node, short_path)
            parts.append(part)
        return '\n\n'.join(parts), short_path_map

    @abstractmethod
    def extract_files(self, s: str) -> list[tuple[str, str]]:
        ...
