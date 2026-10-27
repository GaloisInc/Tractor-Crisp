"""
Rewrite operations using AI agent tools, such as codex-cli
"""

from dataclasses import dataclass
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Sequence, Callable

from fastapi import FastAPI
from pathspec.pathspec import PathSpec

from .. import http_server, llm
from ..config import Config
from ..error import AgentQuotaError, CrispError
from ..mvir import MVIR, TreeNode, FileNode, CodexAgentOpNode
from ..sandbox import run_sandbox, Sandbox

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

def _codex_command(subcmd: str, args: list[str], model: str,
                   effort: str = 'high', use_login: bool = False) -> list[str]:
    cmd = ['codex', subcmd]

    if use_login:
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
        '-c', f'model_reasoning_effort="{effort}"',
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

# Messages codex-cli reports when the API account is out of budget: on lines
# starting with `ERROR:`, or with `--json` (e.g. `codex exec review`), in
# `error`/`turn.failed` events.  Only those lines are checked, since the logs
# also contain arbitrary file contents and command output from the agent.
_QUOTA_ERROR_MARKERS = (
    b'You hit your spend cap',
    b'insufficient_quota',
    b'exceeded your current quota',
)

def _is_error_line(line: bytes) -> bool:
    line = line.lstrip()
    if line.startswith(b'ERROR:'):
        return True
    try:
        ev = json.loads(line)
    except ValueError:
        return False
    return isinstance(ev, dict) and ev.get('type') in ('error', 'turn.failed')

def _is_quota_error(logs: bytes) -> bool:
    return any(
        any(m in line for m in _QUOTA_ERROR_MARKERS) and _is_error_line(line)
        for line in logs.splitlines())

# After the `codex login` runs out of quota, runs use `CRISP_API_KEY` directly
# for this many seconds before trying the login again.
_LOGIN_RETRY_DELAY = 15 * 60
# The `time.monotonic()` time before which runs skip the login.
_login_retry_at = 0.0

HOST_ENV_VAR = 'CRISP_INTERNAL_API_HOST'
KEY_ENV_VAR = 'CRISP_INTERNAL_API_KEY'
PORT_ENV_VAR = 'CRISP_INTERNAL_API_PORT'

class AgentSandbox:
    def __init__(
        self,
        sb: Sandbox,
        mvir: MVIR,
        inputs: dict[str, Input],
        output_filters: dict[str, Callable[[str], bool]],
        cwd: str,
        env: dict,
    ):
        self.sb = sb
        self.mvir = mvir
        self.inputs = inputs
        self.output_filters = output_filters
        self.cwd = cwd
        self.env = env.copy()

    def run(self, cmd):
        return self.sb.run(cmd, cwd=self.cwd, stream=True, env=self.env)

    def run_all(self, cmds):
        logs = None
        for cmd in cmds:
            print(f'run: {shlex.join(cmd)}')
            exit_code, logs2 = self.run(cmd)
            logs = b'\n\n'.join((logs, logs2)) if logs is not None else logs2
            if exit_code != 0:
                break
        return exit_code, logs

    def run_all_with_api_port(self, api_port, api_key, cmds):
        assert HOST_ENV_VAR not in self.env
        self.env[HOST_ENV_VAR] = Sandbox.HOST_ADDR

        assert PORT_ENV_VAR not in self.env
        self.env[PORT_ENV_VAR] = str(api_port)

        assert KEY_ENV_VAR not in self.env
        self.env[KEY_ENV_VAR] = api_key

        r = self.run_all(cmds)

        del self.env[HOST_ENV_VAR]
        del self.env[PORT_ENV_VAR]
        del self.env[KEY_ENV_VAR]
        return r

    def commit_raw_output_files(
        self,
        path_filter: Callable[[str], bool] | None = None,
    ) -> TreeNode:
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
        return self.sb.commit_dir('.', ignore_spec=ignore_spec, path_filter=path_filter)

    def get_input(self, name) -> TreeNode:
        inp = self.inputs[name]
        if isinstance(inp.item, TreeNode) and inp.path == '.':
            return inp.item
        else:
            raise TypeError('TODO: convert Input to TreeNode')

    def get_output(self, name) -> TreeNode:
        path_filter = self.output_filters[name]
        return self.commit_raw_output_files(path_filter)

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
    http_build_app: Callable[[AgentSandbox, FastAPI]] | None = None,
    fallback_cmd: list[str] | None = None,
    op_label: str | None = None,
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
    - http_build_app: If set, this will be called to set up an HTTP API that
      will be available to the agent while it runs.
    - fallback_cmd: If set and `codex_cmd` fails because its account is out
      of quota, run this instead, in the same sandbox after resetting the
      checkout (if `init_git` is set).  After a quota error, later runs skip
      `codex_cmd` and run this directly for `_LOGIN_RETRY_DELAY` seconds.  Either way, the op is labeled `api_key_fallback`.
    - op_label: If set, appended to the op kind in the `op_history` reflog.
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

        asb = AgentSandbox(sb, mvir, inputs, output_filters, cwd, env)

        setup = []
        if init_git:
            setup += [
                ['git', 'init', '--quiet'],
                ['git', 'config', 'set', 'user.name', 'CRISP'],
                ['git', 'config', 'set', 'user.email', 'crisp@localhost'],
                ['git', 'add', '--all'],
                ['git', 'commit', '--quiet', '-m', 'CRISP sandbox baseline'],
            ]
        setup += setup_cmds
        setup += [['mkdir', '-p', codex_dir]]

        all_cmds = []
        # Logs of the last codex run, to check for quota errors.
        codex_logs = b''
        fell_back = False
        def run_cmds(run_all):
            nonlocal codex_logs, fell_back
            global _login_retry_at
            # Skip the login if it ran out of quota recently.
            skip_login = fallback_cmd is not None and time.monotonic() < _login_retry_at
            first_cmd = fallback_cmd if skip_login else codex_cmd
            exit_code, logs = run_all(setup + [first_cmd])
            all_cmds.extend(setup + [first_cmd])
            codex_logs = logs
            fell_back = skip_login
            if (fallback_cmd is not None and not skip_login and exit_code != 0
                    and _is_quota_error(logs)):
                print('codex login is out of quota; rerunning with CRISP_API_KEY '
                    f'and not trying the login again for {_LOGIN_RETRY_DELAY // 60} min')
                _login_retry_at = time.monotonic() + _LOGIN_RETRY_DELAY
                fell_back = True
                # Start over from the inputs; the `.codex/` sessions are
                # ignored by git, so the failed run's log is kept.
                retry = [['git', 'reset', '--hard', '--quiet'],
                    ['git', 'clean', '-fd', '--quiet']] if init_git else []
                retry += [fallback_cmd]
                exit_code, codex_logs = run_all(retry)
                all_cmds.extend(retry)
                logs = b'\n\n'.join((logs, codex_logs))
            if exit_code == 0 and clean_cmds:
                exit_code, clean_logs = run_all(clean_cmds)
                all_cmds.extend(clean_cmds)
                logs = b'\n\n'.join((logs, clean_logs))
            return exit_code, logs

        if http_build_app is not None:
            exit_code, logs = http_server.run_with_callbacks(
                lambda app: http_build_app(asb, app),
                lambda api_port, api_key: run_cmds(
                    lambda cmds: asb.run_all_with_api_port(api_port, api_key, cmds)),
                wait_on_interrupt = not sb.COMMANDS_END_ON_EXIT,
            )
        else:
            exit_code, logs = run_cmds(asb.run_all)

        raw_output_files = asb.commit_raw_output_files()

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
    if fell_back:
        op_label = 'api_key_fallback' if op_label is None else f'{op_label} api_key_fallback'
    reason = n_op.kind if op_label is None else f'{n_op.kind} {op_label}'
    mvir.set_tag('op_history', n_op.node_id(), reason)

    if n_op.exit_code != 0 and _is_quota_error(codex_logs):
        raise AgentQuotaError(
            f'agent invocation failed: API spend cap or quota exhausted '
            f'(exit code {exit_code})', n_op)
    if n_op.exit_code != 0:
        raise CrispError(
            f'agent invocation failed: exit code {exit_code}', n_op)

    return n_op, outputs

def _run_codex(
    cfg: Config,
    mvir: MVIR,
    inputs: dict[str, Input],
    exec_args: list[str],
    model: str,
    effort: str,
    output_filters: dict[str, Callable[[str], bool]],
    **run_agent_kwargs,
) -> tuple[CodexAgentOpNode, dict[str, TreeNode]]:
    """
    Run `codex exec` with `exec_args` via `run_agent`, using the host's `codex
    login` credentials if `cfg.codex_login` is set.  With
    `cfg.codex_login_fallback` too, a run that fails because the login is out
    of quota is rerun with `CRISP_API_KEY`.
    """
    inputs = dict(inputs)
    fallback_cmd = None
    if cfg.codex_login:
        assert 'codex_auth' not in inputs, "duplicate input name 'codex_auth'"
        inputs['codex_auth'] = _codex_auth_input()
        if cfg.codex_login_fallback:
            fallback_cmd = _codex_command('exec', exec_args, model = model,
                effort = effort)
    codex_cmd = _codex_command('exec', exec_args, model = model, effort = effort,
        use_login = cfg.codex_login)
    return run_agent(cfg, mvir, inputs, codex_cmd, output_filters,
        fallback_cmd = fallback_cmd, **run_agent_kwargs)


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
    env: dict | None = None,
    http_build_app: Callable[[AgentSandbox, FastAPI]] | None = None,
    find_unsafe2_json_dir: str | None = None,
    find_unsafe2_src_dir: str | None = None,
    codex_agents: Sequence[str] = (),
    effort: str = 'high',
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
    _add_codex_agent_inputs(inputs, codex_agents)
    # Add `extra_code` last so we can report errors if there are any name
    # conflicts.
    extra_code_files = set()
    for name, tree in extra_code.items():
        assert name not in inputs, f'duplicate input name {name!r}'
        inputs[name] = Input(tree)
        extra_code_files.update(tree.files.keys())

    n_op, outputs = _run_codex(
        cfg, mvir,
        inputs,
        [
            '--dangerously-bypass-approvals-and-sandbox',
            '--skip-git-repo-check',
            prompt,
        ],
        model, effort,
        {
            'code': lambda p: p not in extra_code_files
                and (p in input_code.files or p.endswith('.rs')),
            'plans': lambda p: p not in extra_code_files
                and Path(p).name in ('PLAN.md', 'SAFETY_PLAN.md'),
        },
        cwd = cwd,
        clean_cmds = clean_cmds,
        env = env,
        http_build_app = http_build_app,
    )

    output_code = outputs['code']
    output_plans = outputs['plans']

    return (output_code, output_plans)


def run_review_op(
    cfg: Config,
    mvir: MVIR,
    prompt: str,
    model: str,
    old_code: TreeNode,
    new_code: TreeNode,
    extra_code: TreeNode | dict[str, TreeNode] = {},
    cwd: str = '.',
    env: dict | None = None,
    effort: str = 'high',
) -> CodexAgentOpNode:
    """
    Run `codex exec review` over the change from `old_code` to `new_code`.

    `codex exec review` is used rather than a plain `codex exec` prompt
    because review mode reports findings in a fixed, machine-parseable format
    (it overrides any output convention requested in the prompt).  Codex's own
    sandbox is bypassed (it cannot start inside the CRISP sandbox).

    Review mode can only review a git diff.  The review runs in its own
    fresh sandbox (the rewrite sandbox and its repo are gone by now, and
    `.git/` is never committed to MVIR), so the change is staged as
    uncommitted edits on a baseline commit in a throwaway repo built from
    the MVIR nodes.
    """
    extra_code, env = _normalize_run_args(extra_code, env)

    inputs = {
        'new_code': Input(new_code),
        'old_code': Input(old_code, path = 'crisp_old_code/'),
    }
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

    last_message_path = 'codex_last_message.txt'
    n_op, outputs = _run_codex(
        cfg, mvir,
        inputs,
        [
            'review',
            # Codex's own sandbox (bubblewrap) cannot start inside the CRISP
            # sandbox; the CRISP sandbox is the containment layer.
            '--dangerously-bypass-approvals-and-sandbox',
            # Structured events let us verify the reviewer ran commands.
            '--json',
            '--output-last-message', last_message_path,
            prompt,
        ],
        model, effort,
        {
            'last_message': lambda p: p == last_message_path,
        },
        # Run custom git setup instead of the default
        init_git = False,
        setup_cmds = setup_cmds,
        cwd = cwd,
        env = env,
    )

    return n_op

def review_op_results(mvir: MVIR, n_op: CodexAgentOpNode) -> tuple[Optional[FileNode], bytes, bool]:
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

    n_last_message_tree = mvir.node(n_op.outputs['last_message'])
    if len(n_last_message_tree.files) == 1:
        n_report = mvir.node(n_last_message_tree.sole_file)
    else:
        n_report = None
    logs = n_op.body()
    ran_commands = _review_ran_commands(logs)
    return n_report, logs, ran_commands
