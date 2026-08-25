"""
Rewrite operations using AI agent tools, such as codex-cli
"""

from dataclasses import dataclass
import json
import os
import re
import shlex
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

# Print the warning in red so it stands out
WARNING_TEMPLATE = "\033[31mwarning: {} is being copied into " \
    "the sandbox and could theoretically be leaked " \
    "by commands run by the agent; please make sure " \
    "to set limits on its usage.\033[0m"


@dataclass(frozen = True)
class Input:
    """
    An input to the agent run.  This may be a single file (represented as
    `FileNode`, `bytes`, or `str`) or a complete `TreeNode`.
    """
    item: TreeNode | FileNode | bytes | str
    # Where to put the input.  For single-file inputs, the file is created at
    # this path; for `TreeNode` inputs, the tree is checked out under this
    # directory.
    path: str = '.'
    # If set, add `path` to `.gitignore`.
    git_ignore: bool = False
    # If set, exclude this input from the `inputs` field of the `CodexAgentOpNode`.
    exclude_from_mvir: bool = False


def _normalize_run_args(
    extra_code: TreeNode | dict[str, TreeNode],
    env: dict | None,
) -> tuple[list[TreeNode], dict]:
    """Shared argument normalization for the codex entry points below."""
    if isinstance(extra_code, TreeNode):
        extra_code = {'extra': extra_code}
    return extra_code, {} if env is None else env


_SNAPSHOT_SUFFIX = re.compile(r"^(?P<alias>.+)-\d{4}-\d{2}-\d{2}$")

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


def _codex_auth_input() -> Input:
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

    return Input(
        auth_bytes,
        path = '.codex/auth.json',
        # Avoid persisting auth secrets in MVIR.
        exclude_from_mvir = True,
    )


def _add_codex_agent_inputs(
    inputs: dict[str, Input],
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
    inputs['agent_safety_constraints'] = Input(
        constraints.read_bytes(),
        path = f'.codex/{_CODEX_SAFETY_CONSTRAINTS}',
    )
    for name in agent_names:
        profile = available[name]
        inputs[f'agent_{profile.name}'] = Input(
            profile.read_bytes(),
            path = f'.codex/agents/{profile.name}',
        )


def run_agent(
    cfg: Config,
    mvir: MVIR,
    inputs: dict[str, Input],
    codex_cmd: list[str],
    output_filters: dict[str, Callable[[str], bool]],
    cwd: str = '.',
    init_git: bool = True,
    setup_cmds: list[list[str]] = [],
    clean_cmds: list[list[str]] = [],
    env: dict | None = None,
) -> tuple[CodexAgentOpNode, dict[str, TreeNode]]:
    """
    Run the agent on some input files to produce some outputs.

    For each `(path, item)` value in `inputs`, this checks out `item` at `path`
    within the sandbox.  It then runs the agent with the provided `prompt` and
    `model`.  Finally, for each `name: filter` pair in `output_filters`, it
    returns a dict mapping `name` to a `TreeNode` of all the files matching the
    corresponding `filter`.  For example, if `output_filters` consists of
    `{'code': lambda path: path.endswith('.rs')}`, then the return value of
    this function will be `{'code': tree}` where `tree` contains all the
    outputs with the `.rs` extension.

    The keys used for `inputs` are not significant; they're only present so
    that the resulting MVIR node will include human-readable names.

    Args:
    - inputs: `Input` objects describing files to check out into the sandbox.
      The `str` keys are recorded in the MVIR op node for debugging purposes
      but are otherwise irrelevant.
    - codex_cmd: The main command to run.
    - output_filters: After extracting files from the sandbox, these filters
      are used to gather files into meaningful outputs.  For each filter, the
      `dict` returned by `run_agent` will have a `TreeNode` containing the
      output files that match the filter.  If a file matches multiple filters,
      it will appear in multiple outputs.  The `str` keys are recorded in the
      MVIR op node for debugging purposes but are otherwise irrelevant.
    - init_git: If `True`, set up a git repository before running `codex_cmd`.
    - setup_cmds: Extra setup commands to run before `codex_cmd`.  These happen
      after initializing git, if `init_git` is also set.
    - clean_cmds: Extra cleanup commands to run after `codex_cmd` but before
      extracting outputs.
    - cwd: Working directory (relative to sandbox root) used for all commands.
    """

    if env is None:
        env = {}
    else:
        env = env.copy()

    if 'CRISP_API_KEY' in os.environ:
        print(WARNING_TEMPLATE.format('CRISP_API_KEY'))
        # Env var is set automatically inside `sandbox.run()` if present.
        # TODO: Set it here instead.

    with run_sandbox(cfg, mvir) as sb:
        gitignore_lines = [
            '# Cargo build output',
            'target/',
            '# Codex home; may contain auth.json',
            '.codex/',
        ]

        for k,v in env.items():
            if '%%SANDBOX_ROOT%%' in v:
                env[k] = v.replace('%%SANDBOX_ROOT%%', sb.join('.'))

        # Populate the sandbox with inputs.
        for name, i in inputs.items():
            if isinstance(i.item, TreeNode):
                sb.checkout(i.item, rel_path = i.path)
            elif isinstance(i.item, FileNode):
                sb.checkout_file(i.path, i.item)
            elif isinstance(i.item, bytes):
                sb.checkout_file_untracked(i.path, i.item)
            elif isinstance(i.item, str):
                sb.checkout_file_untracked(i.path, i.item.encode('utf-8'))
            else:
                raise TypeError('expected TreeNode | FileNode | bytes | str, '
                    f'but got {type(i.item)} for {i.path!r}')

            if i.git_ignore:
                gitignore_lines.extend((
                    f'# Input {name!r}',
                    i.path,
                ))

        # Initialize a git repo in `.`.  This lets the agent use `git diff` to
        # examine its changes so far.
        sb.checkout_file_untracked('.gitignore',
            '\n'.join(gitignore_lines).encode('utf-8'))

        # Run the agent.
        codex_dir = sb.join('.codex')
        env.setdefault('CODEX_HOME', codex_dir)

        all_cmds = []
        if init_git:
            all_cmds += [
                ['git', 'init', '--quiet'],
                ['git', 'config', 'set', 'user.name', 'CRISP'],
                ['git', 'config', 'set', 'user.email', 'crisp@localhost'],
                ['git', 'add', '--all'],
                ['git', 'commit', '--quiet', '-m', 'CRISP sandbox baseline'],
            ]
        all_cmds += setup_cmds
        all_cmds += [
            ['mkdir', '-p', codex_dir],
            codex_cmd,
        ]
        all_cmds += clean_cmds

        logs = None
        for cmd in all_cmds:
            print(f'run: {shlex.join(cmd)}')
            exit_code, logs2 = sb.run(cmd, cwd=cwd, stream=True, env=env)
            logs = b'\n\n'.join((logs, logs2)) if logs is not None else logs2
            if exit_code != 0:
                break

        # Gather raw output files.
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

    # Gather input `NodeId`s.
    input_node_ids = {}
    for name, i in inputs.items():
        if i.exclude_from_mvir:
            continue
        if isinstance(i.item, (TreeNode, FileNode)):
            input_node_ids[name] = i.item.node_id()
        elif isinstance(i.item, (bytes, str)):
            input_node_ids[name] = FileNode.new(mvir, i.item).node_id()
        else:
            assert False, f'bad input type {type(i.item)} (should have been caught above)'

    # Group files into outputs according to `output_filters`.
    outputs = {}
    for key, filter_func in output_filters.items():
        files = {}
        for path, node_id in raw_output_files.files.items():
            if filter_func(path):
                files[path] = node_id
        outputs[key] = TreeNode.new(mvir, files = files)

    # Special handling for JSON session files.  We expect to find at most one
    # log from the agent invocation.
    json_session_files = [node_id for path, node_id in raw_output_files.files.items()
        if path.startswith('.codex/sessions/') and path.endswith('.jsonl')]

    # Set the `json_session` metadata field to the session file only if it's
    # unique.  In case of ambiguity, we leave this blank, but any files that
    # were created will be available in `raw_output_files` if needed.
    if len(json_session_files) == 1:
        json_session_node_id = json_session_files[0]
    else:
        json_session_node_id = FileNode.new(mvir, '').node_id()

    n_op = CodexAgentOpNode.new(mvir,
        inputs = input_node_ids,
        outputs = {k: v.node_id() for k,v in outputs.items()},
        cmds = all_cmds,
        exit_code = exit_code,
        raw_output_files = raw_output_files.node_id(),
        json_session = json_session_node_id,
        body = logs if logs is not None else b'',
    )
    # Record operations and timestamps in the `op_history` reflog.
    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)

    if n_op.exit_code != 0:
        raise CrispError(
            f'agent invocation failed: exit code {exit_code}', n_op)

    return n_op, outputs

def run_rewrite(
    cfg: Config,
    mvir: MVIR,
    prompt: str,
    model: str,
    input_code: TreeNode,
    extra_code: TreeNode | dict[str, TreeNode] = {},
    planning_files: TreeNode | None = None,
    unsafe_json: TreeNode | None = None,
    cwd: str = '.',
    clean_cmds: list[list[str]] = [],
    codex_login: bool = False,
    env: dict | None = None,
    find_unsafe2_json_dir: str | None = None,
    find_unsafe2_src_dir: str | None = None,
    codex_agents: Sequence[str] = (),
) -> tuple[TreeNode, TreeNode]:
    extra_code, env = _normalize_run_args(extra_code, env)

    if find_unsafe2_json_dir is not None:
        env['FIND_UNSAFE2_JSON_DIR'] = os.path.join('%%SANDBOX_ROOT%%', find_unsafe2_json_dir)
    if find_unsafe2_src_dir is not None:
        env['FIND_UNSAFE2_SRC_DIR'] = os.path.join('%%SANDBOX_ROOT%%', find_unsafe2_src_dir)

    inputs = {
        'code': Input(input_code),
    }
    if planning_files is not None:
        inputs['plans'] = Input(planning_files)
    if unsafe_json is not None:
        inputs['unsafe_json'] = Input(unsafe_json, git_ignore=True)
    if codex_login:
        inputs['codex_auth'] = _codex_auth_input()
    _add_codex_agent_inputs(inputs, codex_agents)
    # Add `extra_code` last so we can report errors if there are any name
    # conflicts.
    extra_code_files = set()
    for name, tree in extra_code.items():
        assert name not in inputs, f'duplicate input name {name!r}'
        inputs[name] = Input(tree)
        extra_code_files.update(tree.files.keys())

    codex_cmd = _codex_command(cfg, 'exec', [
        '--dangerously-bypass-approvals-and-sandbox',
        '--skip-git-repo-check',
        prompt,
    ], codex_login=codex_login, model=model)

    n_op, outputs = run_agent(
        cfg, mvir,
        inputs,
        codex_cmd,
        {
            'code': lambda p: p not in extra_code_files
                and (p in input_code.files or p.endswith('.rs')),
            'plans': lambda p: p not in extra_code_files
                and Path(p).name in ('PLAN.md', 'SAFETY_PLAN.md'),
        },
        cwd = cwd,
        clean_cmds = clean_cmds,
        env = env,
    )

    output_code = outputs['code']
    output_plans = outputs['plans']

    return (output_code, output_plans)


def run_review(
    cfg: Config,
    mvir: MVIR,
    prompt: str,
    model: str,
    old_code: TreeNode,
    new_code: TreeNode,
    extra_code: TreeNode | dict[str, TreeNode] = {},
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
    the MVIR nodes.  Codex rejects `--uncommitted` when custom review
    instructions are given, so `prompt` itself must direct the reviewer at the
    uncommitted changes.
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

    extra_code, env = _normalize_run_args(extra_code, env)

    inputs = {
        'new_code': Input(new_code),
        'old_code': Input(old_code, path = 'crisp_old_code/'),
    }
    if codex_login:
        inputs['codex_auth'] = _codex_auth_input()
    for name, tree in extra_code.items():
        assert name not in inputs, f'duplicate input name {name!r}'
        inputs[name] = Input(tree)

    setup_cmds = [
        ['git', 'init', '--quiet'],
        ['git', 'config', 'set', 'user.name', 'CRISP'],
        ['git', 'config', 'set', 'user.email', 'crisp@localhost'],
        # `.` contains the new files, and `crisp_old_code/` contains the old
        # ones.  This use of `$GIT_WORK_TREE` adds the old files to the index
        # unprefixed, so the contents of `crisp_old_code/foo/bar.txt` will be
        # added to git under the name `foo/bar.txt`.  Later `git diff` without
        # `$GIT_WORK_TREE` set will compare the committed state against `.`,
        # thus comparing the old code to the new code.
        ['env', 'GIT_WORK_TREE=crisp_old_code', 'git', 'add', '--all'],
        ['git', 'commit', '--quiet', '-m', 'CRISP sandbox baseline'],
        # Old files are no longer needed.
        ['rm', '-rf', 'crisp_old_code'],
    ]

    last_message_path = '.codex/last_message.txt'
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

    n_op, outputs = run_agent(
        cfg, mvir,
        inputs,
        codex_cmd,
        {
            'last_message': lambda p: p == last_message_path,
        },
        # Run custom git setup instead of the default
        init_git = False,
        setup_cmds = setup_cmds,
        cwd = cwd,
        env = env,
    )

    n_last_message_tree = mvir.node(n_op.outputs['last_message'])
    n_last_message = mvir.node(n_last_message_tree.sole_file)
    report_bytes = n_last_message.body().decode('utf-8', errors='replace')
    logs = n_op.body()
    ran_commands = _review_ran_commands(logs)
    return report_bytes, logs, ran_commands
