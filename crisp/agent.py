"""
Rewrite operations using AI agent tools, such as codex-cli
"""

from pathspec.pathspec import PathSpec

from . import llm
from .config import Config
from .error import CrispError
from .mvir import MVIR, TreeNode
from .sandbox import run_sandbox

def _codex_command(subcmd: str, args: list[str]) -> list[str]:
    config_settings = {
        'model_providers.crisp.name': 'crisp',
        'model_providers.crisp.base_url': llm.API_BASE,
        #'model_providers.crisp.api_key': llm.API_KEY or 'sk-no-api-key',
        'model_providers.crisp.env_key': 'CRISP_API_KEY',
        'profiles.crisp.model_provider': 'crisp',
        'profiles.crisp.model': llm.API_MODEL or llm.get_default_model(),
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
    with run_sandbox(cfg, mvir) as sb:
        sb.checkout(input_code)
        sb.checkout(test_code)

        codex_cmd = _codex_command('exec', [
            '--dangerously-bypass-approvals-and-sandbox',
            '--skip-git-repo-check',
            prompt,
        ])
        print(codex_cmd)
        all_cmds = [codex_cmd] + clean_cmds

        exit_code = 0
        logs = b''
        for cmd in all_cmds:
            exit_code, logs2 = sb.run(cmd, cwd=cwd, stream=True)
            logs += logs2

            # TODO: produce an `AgentOpNode`
            # TODO: ensure API key doesn't get included in the AgentOpNode
            if exit_code != 0:
                raise CrispError(f'codex-cli failed: exit code {exit_code}')

        ignore_lines = [
            '__pycache__/',
            'build/',
            'build-ninja/',
            'target/',
        ]
        ignore_spec = PathSpec.from_lines('gitignore', ignore_lines)
        all_output_code = sb.commit_dir('.', ignore_spec=ignore_spec)

        # TODO: also extract json logs produced by codex-cli

    # Take all the files from `all_output_code` except for ones that came from
    # `test_code`.  This allows the agent to add, modify, or remove files from
    # `input_code`.
    # TODO: allow limiting this to specific file extensions or glob patterns
    output_files = {k: v for k,v in all_output_code.files.items()
        if k not in test_code.files}
    output_code = TreeNode.new(mvir, files=output_files)

    return output_code
