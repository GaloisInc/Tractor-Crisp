"""
Sandboxing mechanisms for running LLM-generated code.  This is meant to protect
the user's system if the LLM erroneously generates `rm -rf` or similar.
"""

from abc import ABC, abstractmethod
import os

from ..mvir import FileNode, TreeNode
from . import docker as sandbox_docker
from . import sudo as sandbox_sudo


class Sandbox(ABC):
    @abstractmethod
    def checkout(self, n_tree: TreeNode):
        pass

    @abstractmethod
    def run(self, cmd, shell=False, stream=False) -> tuple[int, str]:
        pass

    @abstractmethod
    def commit_file(self, rel_path: str) -> FileNode:
        pass


match os.environ.get("CRISP_SANDBOX", "docker"):
    case "docker":
        run_sandbox = sandbox_docker.run_work_container
        set_keep = sandbox_docker.set_keep_work_container
    case "sudo":
        run_sandbox = sandbox_sudo.run_sandbox
        set_keep = sandbox_sudo.set_keep_temp_dir
    case x:
        raise ValueError(
            'bad value %r for $CRISP_SANDBOX: expected "docker" or "sudo"' % (x,)
        )
