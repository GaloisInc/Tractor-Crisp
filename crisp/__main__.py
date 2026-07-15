import argparse
import ast
from dataclasses import dataclass
import glob
import json
import os
import pathlib
from pathlib import Path
from pathspec import PathSpec, GitIgnoreSpec
import pathspec.util
import random
import re
import requests
import stat
import subprocess
import sys
import tempfile
import traceback

from . import analysis, inline_errors, llm, safety_history, sandbox
from .analysis import COMPILE_COMMANDS_PATH
from .config import Config
from .error import CrispError
from .mvir import MVIR, NodeId, FileNode, TreeNode, LlmOpNode, \
    TestResultNode, CompileCommandsOpNode, TranspileOpNode, SplitFfiOpNode, \
    CodexAgentOpNode
from .sandbox import run_sandbox
from .work_dir import lock_work_dir, set_keep_work_dir
from .workflow import (
    Workflow, OutOfFuelError, AgentTargetField, AgentTargetFunction,
    AgentTargetOther,
)


ARG_PARSE_EPILOG = '''
In subcommand arguments, a `NODE` can be:
* A tag name, which refers to the most recent reflog entry for that tag
* A hexadecimal ID, or any unique prefix of one
* An expression `EXPR` that resolves to a node ID

An `EXPR` can be:
* Any `NODE`
* `NODE.foo`, which loads `NODE` and retrieves field `foo` from its metadata
* `EXPR[idx]`, which evaluates `EXPR` and then performs a Python indexing
  operation using `idx` (which must be a literal)

For example, if the `current` tag refers to a `TreeNode` containing a
`Cargo.toml` file, then `current.files["Cargo.toml"]` is a valid `NODE` that
refers to the `FileNode` for `Cargo.toml`.  `current.files` is an `EXPR` but
not a `NODE` because it evaluates to a dict rather than a node ID.
'''

def parse_args():
    ap = argparse.ArgumentParser(epilog=ARG_PARSE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', '-c', dest='config_path', default='crisp.toml')
    ap.add_argument('--mvir-storage-dir')
    ap.add_argument('--keep-work-dir', action='store_true',
        help='Preserve the `crisp-storage/work` temp directory.  '
            'Useful for debugging.  You must remove the directory manually '
            'before running further commands.')

    sub = ap.add_subparsers(dest='cmd')

    main = sub.add_parser('main')
    main.add_argument('node', nargs='?', default='c_code')
    main.add_argument('--on-accept', metavar='COMMAND',
        help='Run COMMAND after each accepted CRISP state.')
    main.add_argument('--llm-mode',
        choices=('default', 'no_ffi', 'agent', 'agent_sim_no_tests',
            'agent_rand_target'),
        default='default',
        help='which style of LLM-based rewriting to use')
    main.add_argument('--codex-login', action='store_true',
        help="use the host's `codex login` credentials instead of CRISP_API_KEY; "
            "requires --llm-mode=agent or similar")

    safety_loop = sub.add_parser('safety-loop')
    safety_loop.add_argument('--c-code', default='c_code')
    safety_loop.add_argument('node', nargs='?', default='current')
    safety_loop.add_argument('--llm-mode',
        choices=('default', 'no_ffi', 'agent', 'agent_sim_no_tests',
            'agent_rand_target'),
        default='default',
        help='which style of LLM-based rewriting to use')
    safety_loop.add_argument('--codex-login', action='store_true',
        help="use the host's `codex login` credentials instead of CRISP_API_KEY; "
        "requires --llm-mode=agent or similar")

    repl = sub.add_parser('repl')
    repl.add_argument('--node', '-n', action='append', metavar='[NAME=]NODE',
        help='store NODE in local variable NAME (default: `n`) before starting repl')

    eval_ = sub.add_parser('eval')
    eval_.add_argument('expr')
    eval_.add_argument('--node', '-n', action='append', metavar='[NAME=]NODE',
        help='store NODE in local variable NAME (default: `n`) before evaluating')

    reflog = sub.add_parser('reflog')
    reflog.add_argument('tag', nargs='?', default='current')

    tag = sub.add_parser('tag')
    tag.add_argument('--tag', '-t', default='current')
    tag.add_argument('node')

    show = sub.add_parser('show')
    show.add_argument('node', nargs='?', default='current')
    show.add_argument('--raw', action='store_true')
    show.add_argument('--files', action='store_true',
        help='If the target node is a TreeNode, show all files in the tree.')

    index = sub.add_parser('index')
    index.add_argument('node', nargs='?', default='current')

    safety_history_parser = sub.add_parser(
        'safety-history',
        help='emit completed Codex safety-loop history from MVIR',
    )
    safety_history_selection = safety_history_parser.add_mutually_exclusive_group()
    safety_history_selection.add_argument(
        '--after', metavar='AGENT_OP',
        help='emit only rows after this agent-operation node (exclusive)',
    )
    safety_history_selection.add_argument(
        '--agent-op', metavar='AGENT_OP',
        help='emit exactly this agent-operation row',
    )
    safety_history_parser.add_argument(
        '--format', choices=('json',), default='json',
        help='output format (default: json)',
    )
    safety_history_parser.add_argument(
        '--compact', action='store_true',
        help='emit compact JSON instead of indented JSON',
    )

    commit = sub.add_parser('commit',
        help='import files and directories into MVIR')
    commit.add_argument('--tag', '-t', default='current')
    commit.add_argument('--exclude', action='append', default=[],
        help="don't import files that match this gitignore-style rule")
    commit.add_argument('--ignore-missing', action='store_true',
        help='ignore nonexistent `path` arguments instead of reporting an error')
    commit.add_argument('path', nargs='*')

    checkout = sub.add_parser('checkout')
    checkout.add_argument('node', nargs='?', default='current')
    checkout.add_argument('--path', default='.',
        help='check out the files into this directory')

    git = sub.add_parser('git')
    git.add_argument('-n', '--node', default='current')
    git.add_argument('args', nargs='*')

    sandbox_run = sub.add_parser('sandbox-run')
    sandbox_run.add_argument('run_cmd', nargs='*')
    sandbox_run.add_argument('--checkout', action='append', default=[], metavar='NODE',
        help='check out files from this node into the sandbox')

    args = ap.parse_args()
    AGENT_MODES = ('agent', 'agent_sim_no_tests', 'agent_rand_target')
    if getattr(args, 'codex_login', False) and getattr(args, 'llm_mode', None) not in AGENT_MODES:
        ap.error('--codex-login requires --llm-mode=agent or similar')
    return args


def parse_node_id_arg(mvir, s):
    node_id, _ = parse_node_id_arg_and_check_tag(mvir, s)
    return node_id

HEX_DIGITS_RE = re.compile(r'[0-9a-fA-F]+')
OPERATOR_RE = re.compile(r'\[|\.')

def parse_node_id_expr(mvir: MVIR, node_str: str, expr_suffix: str) -> NodeId:
    """
    Parse a "ref expression" like `a1b2c3.foo` or `tag.bar[0]`.  `node_str`
    should be the base node, like `a1b2c3`, and `expr_suffix` should be the
    rest of the expression.

    The initial value is the `NodeId` obtained by parsing `node_str`.  The
    operations given in `expr_suffix` are then applied.  The supported
    operations are:
    - `.foo`: Take the current value, which must be a `NodeId`, load the
      `Node` with that ID, and look up attribute `foo` on it.
    - `[idx]`: Take the current value and access index `idx` on it.  `idx` can
      be any literal.  For example, `current.files["Cargo.toml"]` is a valid
      ref expression (assuming the tag `current` refers to a `TreeNode`).

    The final value must be a `NodeId`.
    """
    base_node_id = parse_node_id_arg(mvir, node_str)
    # `expr_suffix` will be something like `.foo`, `[0]`, or `.foo[0]`.  Add a
    # variable name to the front to make a complete expression.
    expr_ast = ast.parse('x' + expr_suffix, mode = 'eval')
    def go(a):
        match type(a):
            case ast.Name:
                return base_node_id
            case ast.Subscript:
                x = go(a.value)
                idx = ast.literal_eval(a.slice)
                return x[idx]
            case ast.Attribute:
                x = go(a.value)
                node = mvir.node(x)
                return getattr(node, a.attr)
            case _:
                raise TypeError(f'unsupported expression kind: {a}')
    final = go(expr_ast.body)
    assert isinstance(final, NodeId), \
            f'expected expr {node_str + expr_suffix!r} to produce a NodeId, but got {type(final)}'
    return final

def parse_node_id_arg_and_check_tag(mvir, s):
    """
    Parse `s` as a node ID.  Returns `(s, is_tag)`, where `is_tag` is `True` if
    `s` is a tag name.
    """
    # 1. Try parsing as a `NodeId`.
    if len(s) == 2 * NodeId.LENGTH:
        try:
            node_id = NodeId.from_str(s)
            return (node_id, False)
        except ValueError:
            pass
    # 2. Try parsing as a tag name.
    if mvir.has_tag(s):
        return (mvir.tag(s), True)

    # 3. Try parsing as an expression.
    m = OPERATOR_RE.search(s)
    if m is not None:
        i = m.start()
        return (parse_node_id_expr(mvir, s[:i], s[i:]), False)

    # 4. Try parsing as a prefix of a `NodeId`.
    matches = mvir.node_ids_with_prefix(s)
    if len(matches) == 0:
        raise ValueError('node %r not found' % s)
    elif len(matches) == 1:
        return (matches[0], False)
    else:
        raise ValueError('found multiple nodes with prefix %r: %r' % (s, matches))

def parse_node_id(mvir, s):
    node_id, _is_tag = parse_node_id_arg_and_check_tag(mvir, s)
    return node_id


def do_main(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir, codex_login=args.codex_login)

    c_code_node_id = parse_node_id_arg(mvir, args.node)
    n_c_code = mvir.node(c_code_node_id)

    paths = (Path(path) for path in n_c_code.files.keys())
    num_c_files = sum(1 for path in paths if path.suffix == ".c")

    # Try transpiling with Hayroll first, then fall back to plain C2Rust.  Note
    # that `w.transpile` also checks that the tests pass, so a successful
    # transpile with failing tests counts as a failure here.
    n_code = None
    if n_code is None and num_c_files > 1:
        n_code = w.transpile(
            n_c_code,
            src_loc_annotations=True,
            refactor_transforms=("rename_unnamed", "reorganize_definitions"),
        )
    if n_code is None:
        n_code = w.transpile(n_c_code, src_loc_annotations=True, hayroll=True)
    if n_code is None:
        n_code = w.transpile(n_c_code)
    if n_code is None:
        return
    w.accept(n_code, ('main', 'transpile'))

    n_code = w.split_ffi(n_code)
    if not w.cargo_check_json_op(n_code).passed:
        print('error: build failed after split_ffi')
        return
    if not w.test(n_code, n_c_code):
        print('error: tests failed after split_ffi')
        return
    w.accept(n_code, ('main', 'split_ffi'))

    # Auto-fix compiler warnings (e.g. unused imports) before the safety loop, so
    # they don't pollute every iteration's build output and waste tokens.
    n_code = w.cargo_fix(n_code)
    if not w.cargo_check_json_op(n_code).passed:
        print('error: build failed after cargo fix')
        return
    if not w.test(n_code, n_c_code):
        print('error: tests failed after cargo fix')
        return
    w.accept(n_code, ('main', 'cargo_fix'))

    safety_loop_common(args, cfg, mvir, w, n_code, n_c_code)


def prior_agent_plans(mvir, n_code) -> TreeNode | None:
    # Look up the node which produced `n_code` and return the planning files for that step.
    # This lets a resumed safety-loop pick up the previous `SAFETY_PLAN.md` if it exists.
    matches = [
        ie for ie in mvir.index(n_code.node_id())
        if ie.kind == CodexAgentOpNode.KIND and ie.key == 'new_code'
    ]

    match matches:
        case [ie]:
            op_node = mvir.node(ie.node_id)
            return mvir.node(op_node.planning_files)
        case []:
            return None
        case _:
            raise CrispError(
                f'multiple Codex agent ops produced code node {n_code.node_id()}: '
                + ', '.join(str(ie.node_id) for ie in matches)
            )


@dataclass(frozen = True)
class FuelLimits:
    # Try at most this many times in total to make the code safe.
    safety_tries: int

    # Bail out if the LLM fails to improve safety of the code for several
    # consecutive iterations.  For example, if LLM_SAFETY_TRIES=100 and
    # LLM_SAFETY_MAX_CONSECUTIVE_FAILURES=3, the loop will stop if it makes no
    # progress for 3 iterations in a row, on the assumption that the LLM has
    # gotten stuck somehow, but otherwise will keep going for 100 iteratiors.
    max_consecutive_failures: int

    # Give the agent this many iterations to fix its current target before
    # switching to a new target.  If it succeeds at fixing the current target,
    # the outer loop will pick a new target immediately instead.
    safety_tries_per_target: int

def total_code_size(mvir, n_code):
    total = 0
    for name, file in n_code.files.items():
        if name.endswith('.rs'):
            total += mvir.node(file).body_str().count('\n')
    return total

def get_fuel_limits(mvir, n_code):
    """
    Get `FuelLimits` from the environment, or guess appropriate values based on
    the overall size of `n_code`.
    """
    size = total_code_size(mvir, n_code)
    if size < 2000:
        # B01/B02
        defaults = FuelLimits(
            safety_tries = 8,
            max_consecutive_failures = 3,
            safety_tries_per_target = 2,
        )
    elif size < 20000:
        # P01
        defaults = FuelLimits(
            safety_tries = 45,
            max_consecutive_failures = 5,
            safety_tries_per_target = 3,
        )
    else:
        # P02 - run forever
        defaults = FuelLimits(
            safety_tries = 9999,
            max_consecutive_failures = 9999,
            safety_tries_per_target = 5,
        )
    print(f'code size = {size}')
    print(f'default limits = {defaults!r}')

    return FuelLimits(
        safety_tries = int(
            os.environ.get('LLM_SAFETY_TRIES',
                defaults.safety_tries)),
        max_consecutive_failures = int(
            os.environ.get('LLM_SAFETY_MAX_CONSECUTIVE_FAILURES',
                defaults.max_consecutive_failures)),
        safety_tries_per_target = int(
            os.environ.get('LLM_SAFETY_TRIES_PER_TARGET',
                defaults.safety_tries_per_target)),
    )

def safety_loop_common(args, cfg, mvir, w, n_code, n_c_code):
    limits = get_fuel_limits(mvir, n_code)
    print(f'limits = {limits!r}')

    w.fuel.give(limits.safety_tries)

    best_unsafe_count = None
    consecutive_failures = 0
    n_plans = prior_agent_plans(mvir, n_code) or TreeNode.new(mvir, files={})

    target_goal = None
    target_goal_tries = 0

    prev_fuel = None
    while True:
        unsafe_count = w.count_unsafe2(n_code)
        if unsafe_count == 0:
            break

        # Update consecutive failure count
        if best_unsafe_count is None or unsafe_count < best_unsafe_count:
            best_unsafe_count = unsafe_count
            consecutive_failures = 0
        else:
            # The previous iteration failed to make progress.  (Note the LLM
            # may have run normally and produced working code, but if it didn't
            # improve the unsafe count, we still consider that to be a failed
            # iteration.)
            consecutive_failures += 1
            if consecutive_failures >= limits.max_consecutive_failures:
                print(f'stopping due to {consecutive_failures} consecutive failures')
                break

        # Infinite loop detection
        cur_fuel = w.fuel.fuel
        assert cur_fuel != prev_fuel, 'safety loop ran without consuming any fuel'
        prev_fuel = cur_fuel

        try:
            match args.llm_mode:
                case 'agent':
                    match consecutive_failures:
                        case 0 | 1:
                            suffix = None
                        case 2 | 3:
                            # Previous steps failed to make progress on
                            # `unsafe`.  We've seen the agent sometimes just do
                            # refactoring or other general cleanup that doesn't
                            # directly reduce unsafe.  This is actually
                            # desirable, but if it goes on too long, we add a
                            # reminder to focus on reducing unsafety.
                            suffix = (
                                'Remember, your primary goal is to reduce '
                                'the amount of unsafe code. '
                                'Try to remove at least one unsafe operation '
                                'or `unsafe fn`/`static mut` qualifier '
                                'from the core implementation code.'
                            )
                        case n:
                            # Last-ditch attempt to get the agent to make
                            # progress.  This may be too strongly worded, to
                            # the point of encouraging cheating (such as moving
                            # unsafe operations into FFI wrappers).
                            suffix = (
                                'Remember, your primary goal is to reduce '
                                'the amount of unsafe code. '
                                f'Your past {n} attempts failed to remove '
                                'any unsafe operations. '
                                'You MUST remove at least one unsafe operation '
                                'or `unsafe fn`/`static mut` qualifier '
                                'from the core implementation code '
                                '(NOT from FFI entry points), '
                                'or this run will be terminated.'
                            )

                    n_new_code, n_new_plans = w.do_safety_step_agent(
                        n_code, n_c_code, n_plans,
                        prompt_suffix = suffix)

                case 'agent_rand_target':
                    if (target_goal is None or target_goal_tries == 0
                            or target_goal_is_done(w, n_code, target_goal)):
                        target_goal = pick_target_goal(w, n_code)
                        target_goal_tries = limits.safety_tries_per_target

                    target_goal_tries -= 1

                    n_new_code, n_new_plans = w.do_safety_step_agent(
                        n_code, n_c_code, n_plans,
                        target_goal = target_goal)

                case 'agent_sim_no_tests':
                    n_new_code, n_new_plans = w.do_safety_step_agent_sim_no_tests(
                        n_code, n_c_code, n_plans)

                case 'default':
                    n_new_code = w.do_safety_step_llm(n_code, n_c_code)
                    n_new_plans = n_plans

                case 'no_ffi':
                    n_new_code = w.do_safety_step_llm(n_code, n_c_code, no_ffi = True)
                    n_new_plans = n_plans

                case mode:
                    # `--llm-mode agent` should be handled at a higher level.
                    assert False, f'unexpected llm_mode {mode!r}'

            if n_new_code is not None:
                w.accept(n_new_code, ('main', 'safety', cur_fuel))
                n_code = n_new_code
                n_plans = n_new_plans

        except CrispError as e:
            print(f'{args.llm_mode} safety attempt {cur_fuel} failed: {e}')
            traceback.print_exc()

        except OutOfFuelError as e:
            print(f'exiting due to lack of fuel: {e}')
            break

    print('\n\n')
    print('final code = %s' % n_code.node_id())
    print('final c code = %s' % n_c_code.node_id())
    n_op_test = w.test_op(n_code, n_c_code)
    unsafe_count = w.count_unsafe2(n_code)
    print('final unsafe count = %d' % unsafe_count)
    print('final test exit code = %d' % n_op_test.exit_code)

def pick_target_goal(w, n_code):
    # Choose a `target_goal`.
    function_targets = []
    field_targets = []
    for n_json_file in w.find_unsafe2_json_files(n_code):
        j = n_json_file.body_json()
        function_targets.extend(AgentTargetFunction(k)
            for k,v in j['fns'].items()
            if v['total_unsafe'] > 0 and not v['is_ffi_entry_point'])
        for type_name, j_type in j['types'].items():
            if 'type' in j_type['field_contains_raw_ptr']:
                # This is a type alias, not a struct
                # definition.  To avoid confusing prompting, we
                # omit this from `target_fields`.  (Otherwise
                # we would end up telling the agent to fix the
                # "type" field of a non-struct.)
                continue
            field_targets.extend(AgentTargetField(type_name, k)
                for k,v in j_type['field_contains_raw_ptr'].items()
                if v > 0)

    # Prefer fields over functions by giving them 5x normal
    # weight.
    functions_weight = len(function_targets)
    fields_weight = 5 * len(field_targets)
    target_lists = [function_targets, field_targets]
    weights = [functions_weight, fields_weight]
    if sum(weights) > 0:
        print(f'choosing target with category weights {weights!r}')
        target_list, = random.choices(target_lists, weights)
        target_goal = random.choice(target_list)
    else:
        # We found no fields or functions, but `count_unsafe2`
        # still reported some unsafe code.  Tell the agent to
        # check the entire codebase for leftover unsafety.
        target_goal = AgentTargetOther()

    return target_goal

def target_goal_is_done(w, n_code, target_goal):
    total = 0
    for n_json_file in w.find_unsafe2_json_files(n_code):
        j = n_json_file.body_json()
        match target_goal:
            case AgentTargetField(struct, field):
                j_struct = j['types'].get(struct)
                if j_struct is not None:
                    total += j_struct['field_contains_raw_ptr'].get(field, 0)
            case AgentTargetFunction(func):
                j_func = j['fns'].get(func)
                if j_func is not None:
                    total += j_func['total_unsafe']
            case AgentTargetOther():
                total += j['total_unsafe']
    return total == 0


def do_safety_loop(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir, codex_login=args.codex_login)

    c_code_node_id = parse_node_id_arg(mvir, args.c_code)
    n_c_code = mvir.node(c_code_node_id)

    code_node_id = parse_node_id_arg(mvir, args.node)
    n_code = mvir.node(code_node_id)

    safety_loop_common(args, cfg, mvir, w, n_code, n_c_code)


def repl_locals(args, cfg, mvir, w):
    dct = dict(
        cfg = cfg,
        mvir = mvir,
        w = w,
        node = lambda s: mvir.node(parse_node_id(mvir, s)),
    )

    for x in args.node:
        name, sep, expr = x.partition('=')
        if sep == '':
            name, expr = 'n', x
        assert name not in dct, f'duplicate entry for {name!r}'
        dct[name] = mvir.node(parse_node_id(mvir, expr))

    return dct

def do_repl(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    import code
    code.interact(local = repl_locals(args, cfg, mvir, w))

def do_eval(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    x = eval(args.expr, globals(), repl_locals(args, cfg, mvir, w))
    print(x)

def do_reflog(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    for x in mvir.tag_reflog(args.tag):
        print(x)

def do_tag(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    node_id = parse_node_id_arg(mvir, args.node)
    mvir.set_tag(args.tag, node_id, 'tag')

def do_index(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    node_id = parse_node_id_arg(mvir, args.node)
    for x in mvir.index(node_id):
        print(x)

def do_show(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    node_id = parse_node_id_arg(mvir, args.node)
    print(node_id)
    n = mvir.node(node_id)
    from pprint import pprint
    if not args.raw:
        pprint(n.metadata())
    else:
        pprint(n.read_raw_metadata())
    if not args.files:
        print('---')
        print(n.body().decode('utf-8'))
    else:
        for name, file_node_id in n.files.items():
            print(' --- %s: ---' % name)
            print(mvir.node(file_node_id).body().decode('utf-8'))


def do_safety_history(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    after = parse_node_id_arg(mvir, args.after) if args.after is not None else None
    agent_op = (
        parse_node_id_arg(mvir, args.agent_op)
        if args.agent_op is not None else None
    )
    data = safety_history.build_safety_history(
        mvir, after=after, agent_op=agent_op
    )
    print(json.dumps(data, indent=None if args.compact else 2))

def get_src_paths(cfg):
    files = set(f
        for g in cfg.src_globs
        for f in glob.glob(g, root_dir=cfg.base_dir, recursive=True))
    for name in files:
        path = os.path.join(cfg.base_dir, name)
        yield name, path

def commit_node(mvir, cfg):
    dct = {}
    for name, path in get_src_paths(cfg):
        with open(path, 'rb') as f:
            dct[name] = FileNode.new(mvir, f.read()).node_id()
    return TreeNode.new(mvir, files=dct)

def do_commit(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    all_paths = {}
    def add_path(path):
        abs_path = os.path.abspath(path)
        rel_path = cfg.relative_path(abs_path)
        assert all_paths.get(rel_path, abs_path) == abs_path
        all_paths[rel_path] = abs_path

    ps = GitIgnoreSpec.from_lines(args.exclude)
    for path_arg in args.path:
        if os.path.isdir(path_arg):
            # `ps.match_file` needs the input to have a trailing slash to
            # recognize it as a directory.
            if not path_arg.endswith('/'):
                path_arg += '/'
            if not ps.match_file(path_arg):
                for path in ps.match_tree_files(path_arg, negate = True):
                    add_path(os.path.join(path_arg, path))
        elif os.path.isfile(path_arg):
            if not ps.match_file(path_arg):
                add_path(path_arg)
        else:
            if args.ignore_missing:
                pass
            else:
                raise OSError(f'path {path_arg!r} does not exist')

    dct = {}
    for rel_path, abs_path in all_paths.items():
        assert rel_path not in dct
        with open(abs_path, 'rb') as f:
            n_file = FileNode.new(mvir, f.read())
            print('%s: %s' % (rel_path, n_file.node_id()))
            dct[rel_path] = n_file.node_id()
    n = TreeNode.new(mvir, files=dct)

    mvir.set_tag(args.tag, n.node_id(), 'commit')
    print('committed %s = %s' % (args.tag, n.node_id()))

def do_checkout(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    node_id = parse_node_id_arg(mvir, args.node)
    new_n = mvir.node(node_id)
    if not isinstance(new_n, TreeNode):
        raise TypeError('expected TreeNode, but got %r' % (type(new_n),))

    # Create files matching the new state
    for name, file_node_id in new_n.files.items():
        file_n = mvir.node(file_node_id)
        path = os.path.join(args.path, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(file_n.body())

    print('checked out %s' % new_n.node_id())

def do_git(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    node_id = parse_node_id_arg(mvir, args.node)

    from . import git
    oid = git.render(mvir, mvir.node(node_id))
    env = os.environb.copy()
    env[b'GIT_DIR'] = os.fsencode(git.repo_path(mvir))

    # If the user writes `{}` anywhere in the git args, it will be replaced
    # with the generated git object ID.  Otherwise, the object ID will be
    # appended to the command.
    cmd = ['git'] + args.args
    replaced = False
    for i, arg in enumerate(cmd):
        if '{}' in arg:
            cmd[i] = arg.format(str(oid))
            replaced = True
    if not replaced:
        cmd.append(str(oid))

    os.execvpe('git', cmd, env)

def do_sandbox_run(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')

    with run_sandbox(cfg, mvir) as sb:
        for node_id_str in args.checkout:
            node_id = parse_node_id_arg(mvir, node_id_str)
            sb.checkout(mvir.node(node_id))
        sb.run(args.run_cmd, stream = True)

def main():
    args = parse_args()

    set_keep_work_dir(args.keep_work_dir)
    sandbox.set_keep(args.keep_work_dir)

    cfg_kwargs = {}
    if args.mvir_storage_dir is not None:
        cfg_kwargs['mvir_storage_dir'] = os.path.abspath(args.mvir_storage_dir)
    if getattr(args, 'on_accept', None) is not None:
        cfg_kwargs['on_accept'] = args.on_accept
    cfg = Config.from_toml_file(args.config_path, **cfg_kwargs)

    if args.cmd == 'main':
        do_main(args, cfg)
    elif args.cmd == 'safety-loop':
        do_safety_loop(args, cfg)
    elif args.cmd == 'repl':
        do_repl(args, cfg)
    elif args.cmd == 'eval':
        do_eval(args, cfg)
    elif args.cmd == 'reflog':
        do_reflog(args, cfg)
    elif args.cmd == 'tag':
        do_tag(args, cfg)
    elif args.cmd == 'show':
        do_show(args, cfg)
    elif args.cmd == 'index':
        do_index(args, cfg)
    elif args.cmd == 'safety-history':
        do_safety_history(args, cfg)
    elif args.cmd == 'commit':
        do_commit(args, cfg)
    elif args.cmd == 'checkout':
        do_checkout(args, cfg)
    elif args.cmd == 'git':
        do_git(args, cfg)
    elif args.cmd == 'sandbox-run':
        do_sandbox_run(args, cfg)
    else:
        raise ValueError('unknown command %r' % (args.cmd,))

if __name__ == '__main__':
    main()
