"""
Rewrite operations using AI agent tools, such as codex-cli
"""

from pathspec.pathspec import PathSpec
import os

from . import llm
from .config import Config
from .error import CrispError
from .mvir import MVIR, TreeNode, FileNode, CodexAgentOpNode
from .sandbox import run_sandbox

AGENT_DEFAULT_MODEL = "gpt-5.4-2026-03-05"

def _codex_command(subcmd: str, args: list[str]) -> list[str]:
    config_settings = {
        'model_providers.crisp.name': 'crisp',
        'model_providers.crisp.base_url': llm.API_BASE,
        #'model_providers.crisp.api_key': llm.API_KEY or 'sk-no-api-key',
        'model_providers.crisp.env_key': 'CRISP_API_KEY',
        'profiles.crisp.model_provider': 'crisp',
        'profiles.crisp.model': llm.API_MODEL or AGENT_DEFAULT_MODEL,
        # TODO: figure out the actual context limit and use it here
        'profiles.crisp.context_length': 128 * 1024,
    }
    cmd = ['codex', subcmd]
    for k, v in config_settings.items():
        cmd += ['-c', f'{k}={v}']
    cmd += ['--profile', 'crisp']
    cmd += args

    return cmd

def run_rewrite(
    cfg: Config,
    mvir: MVIR,
    prompt: str,
    input_code: TreeNode,
    test_code: TreeNode,
    cwd: str = '.',
    clean_cmds: list[list[str]] = [],
) -> TreeNode:
    if 'CRISP_API_KEY' in os.environ:
        # Print the warning in red so it stands out
        print("\033[31mwarning: CRISP_API_KEY is being copied into " \
              "the sandbox and could theoretically be leaked " \
              "by commands run by the agent; please make sure " \
              "to set limits on its usage.\033[0m")

    with run_sandbox(cfg, mvir) as sb:
        sb.checkout(input_code)
        sb.checkout(test_code)

        codex_dir = sb.join(".codex")
        mkdir_codex = ['mkdir', '-p', codex_dir]

        codex_cmd = _codex_command('exec', [
            '--dangerously-bypass-approvals-and-sandbox',
            '--skip-git-repo-check',
            prompt,
        ])
        print(codex_cmd)
        all_cmds = [mkdir_codex, codex_cmd] + clean_cmds

        exit_code = 0
        logs = b''
        for cmd in all_cmds:
            exit_code, logs2 = sb.run(cmd, cwd=cwd, stream=True,
                                      env={"CODEX_HOME": codex_dir})
            logs += logs2

            # TODO: ensure API key doesn't get included in the AgentOpNode
            if exit_code != 0:
                raise CrispError(f'codex-cli failed: exit code {exit_code}')

        ignore_lines = [
            '__pycache__/',
            'build/',
            'build-ninja/',
            'target/',
            '.codex/',
            '!.codex/log/',
            '!.codex/sessions/',
        ]
        ignore_spec = PathSpec.from_lines('gitignore', ignore_lines)
        raw_output_files = sb.commit_dir('.', ignore_spec=ignore_spec)

    output_files = {}
    json_session_files = []
    for path, node_id in raw_output_files.files.items():
        if path in test_code.files:
            # This file came from the C code used for testing.  Ignore it.
            pass
        elif path in input_code.files:
            # This is a modified copy of an original input file.
            output_files[path] = node_id
        elif path.endswith('.rs'):
            # In some cases the agent might create a new Rust file, such as
            # when refactoring to create a new module.  Add these files to the
            # main output.
            output_files[path] = node_id
        elif path.startswith('.codex/sessions/') and path.endswith('.jsonl'):
            # This is a Codex session log file.
            json_session_files.append(node_id)

    # Set the `json_session` metadata field to the session file only if it's
    # unique.  In case of ambiguity, we leave this blank, but any files that
    # were created will be available in `raw_output_files` if needed.
    if len(json_session_files) == 1:
        json_session_node_id = json_session_files[0]
    else:
        json_session_node_id = FileNode.new(mvir, '').node_id()

    output_code = TreeNode.new(mvir, files=output_files)
    n_op = CodexAgentOpNode.new(mvir,
        old_code = input_code.node_id(),
        new_code = output_code.node_id(),
        raw_prompt = FileNode.new(mvir, prompt).node_id(),
        exit_code = exit_code,
        raw_output_files = raw_output_files.node_id(),
        json_session = json_session_node_id,
        body = logs,
    )
    # Record operations and timestamps in the `op_history` reflog.
    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)

    return output_code
