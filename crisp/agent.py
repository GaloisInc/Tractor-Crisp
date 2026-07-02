"""
Rewrite operations using AI agent tools, such as codex-cli
"""

import json
import os
from pathlib import Path
import re
from typing import Any

from pathspec.pathspec import PathSpec

from . import llm
from .config import Config
from .error import CrispError
from .mvir import MVIR, TreeNode, FileNode, CodexAgentOpNode
from .sandbox import run_sandbox

AGENT_DEFAULT_MODEL = "gpt-5.5-2026-04-23"
SHORT_RESUME_PROMPT = """
Resume the Rust safety refactoring work.

Continue reducing unsafe Rust while preserving behavior and ABI compatibility.
Use the existing conversation context, current workspace state, and any
SAFETY_PLAN.md notes to choose and complete the next useful step. Before
finishing, update SAFETY_PLAN.md and run the required build and unsafe checks.
""".strip()

_SNAPSHOT_SUFFIX = re.compile(r"^(?P<alias>.+)-\d{4}-\d{2}-\d{2}$")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class CodexAgentError(CrispError):
    def __init__(self, message: str, codex_state: TreeNode):
        super().__init__(message)
        self.codex_state = codex_state


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


def _walk_json_values(x: Any):
    if isinstance(x, dict):
        for v in x.values():
            yield from _walk_json_values(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk_json_values(v)
    elif isinstance(x, str):
        yield x


def _session_ids_from_jsonl(body: bytes) -> set[str]:
    meta_ids = set()
    fallback_ids = set()
    for raw_line in body.splitlines():
        try:
            line = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        if isinstance(line, dict) and line.get('type') == 'session_meta':
            payload = line.get('payload')
            if isinstance(payload, dict):
                session_id = payload.get('id')
                if isinstance(session_id, str) and _UUID_RE.fullmatch(session_id):
                    meta_ids.add(session_id)

        for value in _walk_json_values(line):
            if (m := _UUID_RE.fullmatch(value)) is not None:
                fallback_ids.add(m.group(0))

    return meta_ids or fallback_ids


def _session_id_from_codex_state(mvir: MVIR, codex_state: TreeNode) -> str | None:
    ids = set()
    for path, node_id in codex_state.files.items():
        if not (path.startswith('.codex/sessions/') and path.endswith('.jsonl')):
            continue

        ids.update(_UUID_RE.findall(path))
        ids.update(_session_ids_from_jsonl(mvir.node(node_id).body()))

    match sorted(ids):
        case []:
            return None
        case [session_id]:
            return session_id
        case many:
            print(
                'warning: found multiple Codex session ids in persisted state '
                f'{many}; falling back to `codex exec resume --last --all`')
            return None


def _codex_exec_args(prompt: str, session_id: str | None, resume_last: bool) -> list[str]:
    common_args = [
        '--dangerously-bypass-approvals-and-sandbox',
        '--skip-git-repo-check',
    ]
    if session_id is not None:
        return ['resume', *common_args, session_id, prompt]
    if resume_last:
        return ['resume', *common_args, '--last', '--all', prompt]
    return [*common_args, prompt]


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
    codex_state: TreeNode | None = None,
    resume_prompt: str = 'short',
    env: dict | None = None,
    find_unsafe2_json_dir: str | None = None,
) -> tuple[TreeNode, TreeNode, TreeNode]:
    if resume_prompt not in ('short', 'full'):
        raise ValueError(f'unknown resume_prompt mode {resume_prompt!r}')

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

    with run_sandbox(cfg, mvir) as sb:
        sb.checkout(input_code)
        for n in extra_code:
            sb.checkout(n)
        if planning_files is not None:
            sb.checkout(planning_files)
        if codex_state is not None:
            sb.checkout(codex_state)

        if codex_login:
            print(WARNING_TEMPLATE.format('codex\'s login session (`auth.json`)'))
            _inject_codex_auth(sb)

        codex_dir = sb.join(".codex")
        mkdir_codex = ['mkdir', '-p', codex_dir]

        session_id = None
        resume_last = False
        if codex_state is not None and codex_state.files:
            session_id = _session_id_from_codex_state(mvir, codex_state)
            resume_last = session_id is None
        is_resume = session_id is not None or resume_last

        codex_prompt = prompt
        if is_resume and resume_prompt == 'short':
            codex_prompt = SHORT_RESUME_PROMPT

        if codex_state is None:
            print('codex session: fresh (--persist-codex-session disabled)')
        elif session_id is not None:
            print(f'codex session: resume {session_id}')
        elif resume_last:
            print('codex session: resume --last --all')
        else:
            print('codex session: fresh (no prior session state)')
        if is_resume:
            print(f'codex resume prompt: {resume_prompt}')

        codex_cmd = _codex_command(
            'exec',
            _codex_exec_args(codex_prompt, session_id, resume_last),
            codex_login=codex_login,
        )
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
                break

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
    output_codex_state_files = {}
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
            output_codex_state_files[path] = node_id
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
    output_codex_state = TreeNode.new(mvir, files=output_codex_state_files)
    n_op = CodexAgentOpNode.new(mvir,
        old_code = input_code.node_id(),
        new_code = output_code.node_id(),
        raw_prompt = FileNode.new(mvir, codex_prompt).node_id(),
        exit_code = exit_code,
        raw_output_files = raw_output_files.node_id(),
        json_session = json_session_node_id,
        planning_files = output_plans.node_id(),
        body = logs,
    )
    # Record operations and timestamps in the `op_history` reflog.
    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)

    print(f'codex session files committed: {len(output_codex_state_files)}')

    if exit_code != 0:
        raise CodexAgentError(
            f'codex-cli failed: exit code {exit_code}',
            output_codex_state,
        )

    return (output_code, output_plans, output_codex_state)
