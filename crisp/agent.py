"""
Rewrite operations using AI agent tools, such as codex-cli
"""

import os
from pathlib import Path
import re
import shlex

from pathspec.pathspec import PathSpec

from . import llm
from .config import Config
from .error import CrispError
from .mvir import MVIR, TreeNode, FileNode, CodexAgentOpNode
from .sandbox import run_sandbox

AGENT_DEFAULT_MODEL = "gpt-5.5-2026-04-23"
BASELINE_DIR = '/tmp/crisp-baseline'

_SNAPSHOT_SUFFIX = re.compile(r"^(?P<alias>.+)-\d{4}-\d{2}-\d{2}$")

def _snapshot_to_family_alias(model: str) -> str:
    """
    Convert a pinned snapshot model ID like:
        gpt-5.4-2026-03-05 -> gpt-5.4
    """
    m = _SNAPSHOT_SUFFIX.match(model)
    return m.group("alias") if m else model

def _codex_command(subcmd: str, args: list[str], codex_login: bool = False) -> list[str]:
    cmd = ['codex', subcmd]

    if codex_login:
        # Use the host's `codex login` credentials (auth.json).  We only
        # override the model; everything else uses codex's defaults.
        # The --model flag does not support snapshot-style model identifiers so
        # we attempt to convert snapshots to model family aliases.
        model = _snapshot_to_family_alias(llm.API_MODEL or AGENT_DEFAULT_MODEL)
        cmd += ['--model', model]
    else:
        config_settings = {
            'model_providers.crisp.name': 'crisp',
            'model_providers.crisp.base_url': llm.API_BASE,
            #'model_providers.crisp.api_key': llm.API_KEY or 'sk-no-api-key',
            'model_providers.crisp.env_key': 'CRISP_API_KEY',
            'model_provider': 'crisp',
            'model': llm.API_MODEL or AGENT_DEFAULT_MODEL,
            # TODO: OpenAI pricing is based on input and output tokens, with 
            # long context tokens costing twice as much as short context ones.
            # We might want to set limits to avoid the long context pricing.
            #
            # Example config limits for gpt-5.5:  
            #
            # 'model_context_window': 272000
            # 'model_auto_compact_token_limit' = 240000
        }
        for k, v in config_settings.items():
            cmd += ['-c', f'{k}={v}']

    # Fast (aka. priority) mode delivers 1.5 faster tokens at 2.5x credit use [0].
    # The service tier selection mechanism can be entirely disabled by setting 
    # `fast_mode == false` [1].
    #
    # [0]: https://developers.openai.com/codex/speed
    # [1]: https://github.com/openai/codex/blob/main/codex-rs/tui/src/service_tier_resolution.rs#L18
    cmd += [
        '-c', 'model_reasoning_effort="high"',
        '-c', 'features.fast_mode=false',
    ]
    
    cmd += args
    return cmd

def _inject_codex_auth(sb):
    """Copy the host's ``auth.json`` into the container's work
    directory so that codex-cli can authenticate using the host's
    ``codex login`` session.

    The file only lives for the lifetime of the container and is never
    written to MVIR.  The ``.codex/`` ignore pattern in ``run_rewrite``
    also ensures it is excluded from ``commit_dir`` output.
    """

    codex_home = os.getenv('CODEX_HOME')
    if codex_home is None:
        codex_home = os.path.expanduser('~/.codex')

    host_auth = os.path.join(codex_home, 'auth.json')
    if not os.path.isfile(host_auth):
        raise CrispError(
            '--codex-login requires a valid codex login session; '
            'run `codex login` first')

    with open(host_auth, 'rb') as f:
        auth_bytes = f.read()

    sb.checkout_file_untracked('.codex/auth.json', auth_bytes)


def _normalize_baseline_path(path: str) -> str:
    path = os.path.normpath(path)
    if os.path.isabs(path) or path.startswith(os.pardir + os.sep) or path == os.pardir:
        raise ValueError(f'baseline path must be relative to the sandbox work dir: {path!r}')
    return path


def _with_baseline_setup(sb, cmd: list[str], baseline_paths: list[str]) -> list[str]:
    if len(baseline_paths) == 0:
        return cmd

    script_lines = [f'rm -rf {shlex.quote(BASELINE_DIR)}']
    for path in baseline_paths:
        path = _normalize_baseline_path(path)
        src = sb.join(path)
        dest = os.path.join(BASELINE_DIR, path)
        script_lines.append(f'mkdir -p {shlex.quote(os.path.dirname(dest))}')
        script_lines.append(f'cp -a {shlex.quote(src)} {shlex.quote(dest)}')
    script_lines.append(f'exec {shlex.join(cmd)}')

    return ['sh', '-c', ' && '.join(script_lines)]


def run_rewrite(
    cfg: Config,
    mvir: MVIR,
    prompt: str,
    input_code: TreeNode,
    extra_code: TreeNode | list[TreeNode] = [],
    planning_files: TreeNode | None = None,
    cwd: str = '.',
    clean_cmds: list[list[str]] = [],
    codex_login: bool = False,
    env: dict | None = None,
    find_unsafe2_json_dir: str | None = None,
    baseline_paths: list[str] | None = None,
) -> tuple[TreeNode, TreeNode]:
    # Print the warning in red so it stands out
    WARNING_TEMPLATE = "\033[31mwarning: {} is being copied into " \
        "the sandbox and could theoretically be leaked " \
        "by commands run by the agent; please make sure " \
        "to set limits on its usage.\033[0m"


    if 'CRISP_API_KEY' in os.environ:
        print(WARNING_TEMPLATE.format('CRISP_API_KEY'))

    if isinstance(extra_code, TreeNode):
        extra_code = [extra_code]

    if env is None:
        env = {}
    if baseline_paths is None:
        baseline_paths = []

    with run_sandbox(cfg, mvir) as sb:
        sb.checkout(input_code)
        for n in extra_code:
            sb.checkout(n)
        if planning_files is not None:
            sb.checkout(planning_files)

        if codex_login:
            print(WARNING_TEMPLATE.format('codex\'s login session (`auth.json`)'))
            _inject_codex_auth(sb)

        codex_dir = sb.join(".codex")
        mkdir_codex = ['mkdir', '-p', codex_dir]

        codex_cmd = _codex_command('exec', [
            '--dangerously-bypass-approvals-and-sandbox',
            '--skip-git-repo-check',
            prompt,
        ], codex_login=codex_login)
        codex_cmd = _with_baseline_setup(sb, codex_cmd, baseline_paths)
        print(codex_cmd)
        all_cmds = [mkdir_codex, codex_cmd] + clean_cmds

        if 'CODEX_HOME' not in env:
            env['CODEX_HOME'] = codex_dir
        if find_unsafe2_json_dir is not None:
            env['FIND_UNSAFE2_JSON_DIR'] = sb.join(find_unsafe2_json_dir)

        exit_code = 0
        logs = b''
        for cmd in all_cmds:
            exit_code, logs2 = sb.run(cmd, cwd=cwd, stream=True, env=env)
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
    output_plan_files = {}
    for path, node_id in raw_output_files.files.items():
        if any(path in n.files for n in extra_code):
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
        elif Path(path).name in ['PLAN.md', 'SAFETY_PLAN.md']:
            # if the agent created a SAFETY_PLAN.md file, carry it over to future steps but
            # don't include it in the main output since it's not source code.
            output_plan_files[path] = node_id

    # Set the `json_session` metadata field to the session file only if it's
    # unique.  In case of ambiguity, we leave this blank, but any files that
    # were created will be available in `raw_output_files` if needed.
    if len(json_session_files) == 1:
        json_session_node_id = json_session_files[0]
    else:
        json_session_node_id = FileNode.new(mvir, '').node_id()

    output_code = TreeNode.new(mvir, files=output_files)
    output_plans = TreeNode.new(mvir, files=output_plan_files)
    n_op = CodexAgentOpNode.new(mvir,
        old_code = input_code.node_id(),
        new_code = output_code.node_id(),
        raw_prompt = FileNode.new(mvir, prompt).node_id(),
        exit_code = exit_code,
        raw_output_files = raw_output_files.node_id(),
        json_session = json_session_node_id,
        planning_files = output_plans.node_id(),
        body = logs,
    )
    # Record operations and timestamps in the `op_history` reflog.
    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)

    return (output_code, output_plans)
