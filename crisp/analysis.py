from functools import wraps
import inspect
import os
import subprocess
import typing

from .config import Config
from .mvir import MVIR, NodeId, Node, TreeNode, TestResultNode, \
        CompileCommandsOpNode
from .work_container import WorkContainer, run_work_container


def _as_node_id(x):
    if isinstance(x, NodeId):
        return x
    else:
        return x.node_id()

def analysis(f):
    """
    Decorator for analysis functions, whose results are cached in the MVIR
    storage.

    Expected usage:
    ```
    @analysis
    def my_analysis(cfg: Config, mvir: MVIR, code: TreeNode, arg: str) -> MyAnalysisNode:
        ...
    ```

    Some arguments are handled specially:
    * The function should take an `MVIR` argument, which is used by the
      decorator.
    * Any `Config` or `WorkContainer` arguments are ignored by the decorator.
    * The first `Node` or `NodeId` argument will be looked up in the MVIR index
      to find cached results.  This argument is otherwise treated normally.

    The remaining arguments must be a subset of the fields of the return type
    (which must be a `Node`).  When the decorated function is called, the
    decorator will look for an existing MVIR node whose field values match the
    arguments.  If a matching node exists, the decorator will return that node;
    otherwise, it will call the function to create one.
    """
    sig = inspect.signature(f)

    node_type = sig.return_annotation
    assert isinstance(node_type, type) and issubclass(node_type, Node), \
        'expected return type to be a Node subclass, but got %r' % node_type
    node_fields = typing.get_type_hints(node_type)

    mvir_param_name = None
    index_param_name = None
    match_fields = set()
    for param in sig.parameters.values():
        param_type = param.annotation
        if param_type in (Config, WorkContainer):
            continue
        if param_type is MVIR:
            mvir_param_name = param.name
            continue
        if param.name not in node_fields:
            raise AttributeError('argument name %r does not match any field name in %r' %
                (param.name, node_type))
        if index_param_name is None:
            if param_type is NodeId \
                    or (isinstance(param_type, type) and issubclass(param_type, Node)):
                index_param_name = param.name
        match_fields.add(param.name)
    if mvir_param_name is None:
        raise AttributeError('no MVIR argument found in signature')
    if index_param_name is None:
        raise AttributeError('no Node or NodeId argument found in signature')

    @wraps(f)
    def g(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()

        def arg_matches(n, k):
            value = bound.arguments[k]
            if isinstance(value, Node):
                value = value.node_id()
            return value == getattr(n, k)

        mvir = bound.arguments[mvir_param_name]
        index_node_id = _as_node_id(bound.arguments[index_param_name])
        found = []
        for entry in mvir.index(index_node_id):
            if entry.kind != node_type.KIND or entry.key != index_param_name:
                continue
            candidate_n = mvir.node(entry.node_id)
            assert isinstance(candidate_n, node_type)
            if not all(arg_matches(candidate_n, k) for k in match_fields):
                continue
            found.append(candidate_n)

        if len(found) == 1:
            print('found %s' % found[0].node_id())
            return found[0]
        elif len(found) == 0:
            print('run %s' % (f,))
            n = f(*args, **kwargs)
            assert isinstance(n, node_type)
            for k in match_fields:
                assert arg_matches(n, k), \
                    'value mismatch on field %r: %r != %r' % (
                        k, getattr(n, k), bound.arguments[k])
            return n
        else:
            raise ValueError('found multiple index entries matching %r' %
                (bound.arguments,))
    return g

@analysis
def run_tests(cfg: Config, mvir: MVIR,
        code: TreeNode, test_code: TreeNode, cmd: str) -> TestResultNode:
    with run_work_container(cfg, mvir) as wc:
        wc.checkout(code)
        wc.checkout(test_code)

        exit_code, logs = wc.run(cmd, shell=True, stream=True)

    n = TestResultNode.new(
            mvir,
            code = code.node_id(),
            test_code = test_code.node_id(),
            cmd = cmd,
            exit_code = exit_code,
            body = logs,
            )
    mvir.set_tag('test_results', n.node_id(), None)
    if n.passed:
        mvir.set_tag('test_passed', n.node_id(), None)
    return n

# We always check out the compile_commands.json at a consistent path, in case
# it contains relative paths.
COMPILE_COMMANDS_PATH = 'build/compile_commands.json'

@analysis
def _cc_cmake_impl(cfg: Config, mvir: MVIR, wc: WorkContainer,
        c_code: TreeNode, cmd: list[str]) -> CompileCommandsOpNode:
    wc.checkout(c_code)
    exit_code, logs = wc.run(cmd)

    if exit_code == 0:
        n_cc = wc.commit_file(COMPILE_COMMANDS_PATH)
    else:
        n_cc = None
    n_cc_id = n_cc.node_id() if n_cc is not None else None

    n_op = CompileCommandsOpNode.new(
        mvir,
        body = logs,
        c_code = c_code.node_id(),
        cmd = cmd,
        exit_code = exit_code,
        compile_commands = n_cc_id,
        )
    return n_op

@analysis
def cc_cmake(cfg: Config, mvir: MVIR, c_code: TreeNode) -> CompileCommandsOpNode:
    with run_work_container(cfg, mvir) as wc:
        src_dir = wc.join(cfg.relative_path(cfg.transpile.cmake_src_dir))
        build_dir = wc.join(os.path.dirname(COMPILE_COMMANDS_PATH))
        cmd = ['cmake', '-B', build_dir, '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON', src_dir]
        n_op = _cc_cmake_impl(cfg, mvir, wc, c_code, cmd)

    mvir.set_tag('op_history', n_op.node_id(), n_op.kind)
    if n_op.compile_commands is not None:
        mvir.set_tag('compile_commands', n_op.compile_commands, n_op.kind)
    return n_op
