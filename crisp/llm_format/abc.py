from abc import ABCMeta, abstractmethod
from ..mvir import FileNode

class LLMFileFormat(metaclass = ABCMeta):
    @abstractmethod
    def get_output_instructions(self) -> str:
        ...

    @abstractmethod
    def emit_file(self, n: FileNode, path: str) -> str:
        ...

    @abstractmethod
    def extract_files(self, s: str) -> list[tuple[str, str]]:
        ...
