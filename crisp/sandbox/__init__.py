"""
Sandboxing mechanisms for running LLM-generated code.  This is meant to protect
the user's system if the LLM erroneously generates `rm -rf` or similar.
"""

import os

from . import docker as sandbox_docker
from . import sudo as sandbox_sudo

match os.environ.get("CRISP_SANDBOX", "docker"):
    case "docker":
        run_sandbox = sandbox_docker.run_work_container
        Sandbox = sandbox_docker.WorkContainer
        set_keep = sandbox_docker.set_keep_work_container
    case "sudo":
        run_sandbox = sandbox_sudo.run_sandbox
        Sandbox = sandbox_sudo.SudoSandbox
        set_keep = sandbox_sudo.set_keep_temp_dir
    case x:
        raise ValueError(
            'bad value %r for $CRISP_SANDBOX: expected "docker" or "sudo"' % (x,)
        )
