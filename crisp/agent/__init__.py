"""
Rewrite operations using AI agent tools, such as codex-cli
"""

import json
import os
import re
from pathlib import Path
from typing import Sequence

from pathspec.pathspec import PathSpec

from .. import llm
from ..config import Config
from ..error import CrispError
from ..mvir import MVIR, TreeNode, FileNode, CodexAgentOpNode
from ..sandbox import run_sandbox

# Repo-side agent assets; installed into the sandbox as `.codex/`, the
# directory codex-cli searches for project-level agents and instructions.
_CODEX_ASSET_DIR = Path(__file__).parent / 'codex'
_CODEX_SAFETY_CONSTRAINTS = 'safety_constraints.md'

PLANNING_CODEX_AGENTS = (
    'collections_analyst',
    'ffi_abi_analyst',
    'libc_analyst',
    'macro_analyst',
    'ownership_analyst',
    'strings_analyst',
)

_SNAPSHOT_SUFFIX = re.compile(r"^(?P<alias>.+)-\d{4}-\d{2}-\d{2}$")

# Print the warning in red so it stands out
WARNING_TEMPLATE = "\033[31mwarning: {} is being copied into " \
    "the sandbox and could theoretically be leaked " \
    "by commands run by the agent; please make sure " \
    "to set limits on its usage.\033[0m"


def _snapshot_to_family_alias(model: str) -> str:
    """
    Convert a pinned snapshot model ID like:
        gpt-5.4-2026-03-05 -> gpt-5.4
    """
    m = _SNAPSHOT_SUFFIX.match(model)
    return m.group("alias") if m else model

def _codex_command(cfg: Config, subcmd: str, args: list[str],
                   model: str, codex_login: bool = False) -> list[str]:
    cmd = ['codex', subcmd]

    if codex_login:
        # Use the host's `codex login` credentials (auth.json).  We only
        # override the model; everything else uses codex's defaults.
        # The --model flag does not support snapshot-style model identifiers so
        # we attempt to convert snapshots to model family aliases.
        model = _snapshot_to_family_alias(llm.API_MODEL or model)
        cmd += ['--model', model]
    else:
        config_settings = {
            'model_providers.crisp.name': 'crisp',
            'model_providers.crisp.base_url': llm.API_BASE,
            #'model_providers.crisp.api_key': llm.API_KEY or 'sk-no-api-key',
            'model_providers.crisp.env_key': 'CRISP_API_KEY',
            'model_provider': 'crisp',
            'model': llm.API_MODEL or model,
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

def _checkout_bytes(sb, mvir: MVIR, rel_path: str, body: bytes):
    """Add an ephemeral file to any supported CRISP sandbox."""
    sb.checkout_file(rel_path, FileNode.new(mvir, body))


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

    # Do not create an MVIR FileNode for credentials: that would persist the
    # secret in CRISP's content-addressed storage.
    sb.checkout_file_untracked('.codex/auth.json', auth_bytes)


def _inject_codex_agents(
    sb,
    mvir: MVIR,
    agent_names: Sequence[str],
):
    """Install selected agent profiles from `codex/` into the sandbox's
    `.codex/`, where codex-cli discovers them."""
    if not agent_names:
        return

    available = {path.stem: path for path in _CODEX_ASSET_DIR.glob('*.toml')}
    unknown = sorted(set(agent_names) - available.keys())
    if unknown:
        raise CrispError(
            f'unknown Codex agent profile(s): {", ".join(unknown)}')

    constraints = _CODEX_ASSET_DIR / _CODEX_SAFETY_CONSTRAINTS
    _checkout_bytes(
        sb,
        mvir,
        f'.codex/{_CODEX_SAFETY_CONSTRAINTS}',
        constraints.read_bytes(),
    )
    for name in agent_names:
        profile = available[name]
        _checkout_bytes(
            sb,
            mvir,
            f'.codex/agents/{profile.name}',
            profile.read_bytes(),
        )


def run_rewrite(
    cfg: Config,
    mvir: MVIR,
    prompt: str,
    model: str,
    input_code: TreeNode,
    extra_code: TreeNode | list[TreeNode] = [],
    planning_files: TreeNode | None = None,
    cwd: str = '.',
    clean_cmds: list[list[str]] = [],
    codex_login: bool = False,
    env: dict | None = None,
    find_unsafe2_json_dir: str | None = None,
    codex_agents: Sequence[str] = (),
) -> tuple[TreeNode, TreeNode]:
    if 'CRISP_API_KEY' in os.environ:
        print(WARNING_TEMPLATE.format('CRISP_API_KEY'))

    if isinstance(extra_code, TreeNode):
        extra_code = [extra_code]

    if env is None:
        env = {}

    with run_sandbox(cfg, mvir) as sb:
        sb.checkout(input_code)
        for n in extra_code:
            sb.checkout(n)
        if planning_files is not None:
            sb.checkout(planning_files)

        # Each agent turn gets a fresh sandbox, so make the entire sandbox
        # (including input_code, unsafety JSON, and planning_files) the baseline
        # of a small local repository.  The repository is rooted at `.` even
        # when Codex runs in a narrower `cwd`; Git will find the parent repo.
        # This lets the agent use ordinary Git commands (especially `git diff`)
        # to inspect its edits.  `git commit -a` alone does not include the
        # initially untracked files, hence the explicit add before creating the
        # baseline commit.
        gitignore_lines = [
            '# Cargo build output',
            'target/',
            '# Codex home; may contain auth.json',
            '.codex/',
        ]
        if find_unsafe2_json_dir is not None:
            gitignore_lines.extend([
                '# CRISP unsafe-analysis results',
                f'{find_unsafe2_json_dir.rstrip("/")}/',
            ])
        gitignore_file = FileNode.new(
            mvir, '\n'.join(gitignore_lines) + '\n')
        sb.checkout_file('.gitignore', gitignore_file)
        init_git_repo = (
            'git init -q && git add --all && '
            'git -c user.name=CRISP -c user.email=crisp@localhost '
            'commit --quiet -m "CRISP sandbox baseline"')
        exit_code, logs = sb.run(init_git_repo, cwd='.', shell=True, stream=True)

        if exit_code == 0:
            _inject_codex_agents(sb, mvir, codex_agents)

            if codex_login:
                print(WARNING_TEMPLATE.format('codex\'s login session (`auth.json`)'))
                _inject_codex_auth(sb)

            codex_dir = sb.join(".codex")
            mkdir_codex = ['mkdir', '-p', codex_dir]

            codex_cmd = _codex_command(cfg, 'exec', [
                '--dangerously-bypass-approvals-and-sandbox',
                '--skip-git-repo-check',
                prompt,
            ], codex_login=codex_login, model=model)
            print(codex_cmd)
            all_cmds = [mkdir_codex, codex_cmd] + clean_cmds

            if 'CODEX_HOME' not in env:
                env['CODEX_HOME'] = codex_dir
            if find_unsafe2_json_dir is not None:
                env['FIND_UNSAFE2_JSON_DIR'] = sb.join(find_unsafe2_json_dir)

            for cmd in all_cmds:
                exit_code, logs2 = sb.run(cmd, cwd=cwd, stream=True, env=env)
                logs += logs2

                # TODO: ensure API key doesn't get included in the AgentOpNode
                if exit_code != 0:
                    break

        ignore_lines = [
            '.git/',
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

    if exit_code != 0:
        raise CrispError(
            f'agent invocation failed: exit code {exit_code}', n_op)

    return (output_code, output_plans)


def run_review(
    cfg: Config,
    mvir: MVIR,
    prompt: str,
    model: str,
    old_code: TreeNode,
    new_code: TreeNode,
    extra_code: TreeNode | list[TreeNode] = [],
    cwd: str = '.',
    codex_login: bool = False,
    env: dict | None = None,
) -> tuple[str, bytes, bool]:
    """
    Run `codex exec review` over the change from `old_code` to `new_code` and
    return the reviewer's final message, the full log output, and whether the
    reviewer successfully ran at least one command (evidence that it actually
    inspected the change rather than answering blind).

    `codex exec review` is used rather than a plain `codex exec` prompt
    because review mode reports findings in a fixed, machine-parseable format
    (it overrides any output convention requested in the prompt).  Codex's own
    sandbox is bypassed (it cannot start inside the CRISP sandbox).

    Review mode can only review a git diff.  The review runs in its own
    fresh sandbox (the rewrite sandbox and its repo are gone by now, and
    `.git/` is never committed to MVIR), so the change is staged as
    uncommitted edits on a baseline commit in a throwaway repo built from
    the MVIR nodes; nothing is committed back to MVIR.  Codex rejects 
    `--uncommitted` when custom review instructions are given, so `prompt`
    itself must direct the reviewer at the uncommitted changes.
    """
    def _review_ran_commands(logs: bytes) -> bool:
        """True iff the codex `--json` event stream in `logs` shows at
           least one successfully executed command."""
        for line in logs.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            item = ev.get('item')
            if (ev.get('type') == 'item.completed'
                    and isinstance(item, dict)
                    and item.get('type') == 'command_execution'
                    and item.get('exit_code') == 0):
                return True
        return False

    if 'CRISP_API_KEY' in os.environ:
        print(WARNING_TEMPLATE.format('CRISP_API_KEY'))

    if isinstance(extra_code, TreeNode):
        extra_code = [extra_code]

    if env is None:
        env = {}

    with run_sandbox(cfg, mvir) as sb:
        sb.checkout(old_code)
        for n in extra_code:
            sb.checkout(n)
        # Keep sandbox-only files out of the reviewed diff.
        _checkout_bytes(sb, mvir, '.gitignore', b'.codex/\ntarget/\n')

        if codex_login:
            print(WARNING_TEMPLATE.format('codex\'s login session (`auth.json`)'))
            _inject_codex_auth(sb)

        codex_dir = sb.join('.codex')
        last_message_path = sb.join('.codex/last_message.txt')

        setup_cmds = [
            ['mkdir', '-p', codex_dir],
            ['git', 'init', '-q'],
            ['git', 'add', '-A'],
            ['git', '-c', 'user.name=crisp', '-c', 'user.email=crisp@localhost',
                'commit', '-qm', 'baseline'],
        ]
        deleted_files = [path for path in old_code.files if path not in new_code.files]
        if deleted_files:
            setup_cmds.append(['rm', '-f'] + deleted_files)
        for cmd in setup_cmds:
            exit_code, logs = sb.run(cmd, cwd=cwd, env=env)
            if exit_code != 0:
                raise CrispError(f'{cmd[0]} failed: exit code {exit_code}: {logs!r}')
        sb.checkout(new_code)

        codex_cmd = _codex_command(cfg, 'exec', [
            'review',
            # Codex's own sandbox (bubblewrap) cannot start inside the CRISP
            # sandbox; the CRISP sandbox is the containment layer.
            '--dangerously-bypass-approvals-and-sandbox',
            # Structured events let us verify the reviewer ran commands.
            '--json',
            '--output-last-message', last_message_path,
            prompt,
        ], codex_login=codex_login, model=model)
        print(codex_cmd)

        if 'CODEX_HOME' not in env:
            env['CODEX_HOME'] = codex_dir

        exit_code, logs = sb.run(codex_cmd, cwd=cwd, stream=True, env=env)
        if exit_code != 0:
            raise CrispError(f'codex-cli failed: exit code {exit_code}')

        cat_exit_code, report_bytes = sb.run(['cat', last_message_path], cwd=cwd, env=env)
        if cat_exit_code != 0:
            print(f'warning: failed to read reviewer message: {report_bytes!r}')
            report_bytes = b''

    ran_commands = _review_ran_commands(logs)
    return report_bytes.decode('utf-8', errors='replace'), logs, ran_commands

