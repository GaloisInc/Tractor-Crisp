"""
Report which files and functions the AI improved in each iteration of a
refactoring run.
"""

import os
import sys

# When run directly with `python3 history_progress.py`, dispatch to `uv run`
# instead, so that CRISP and the necessary dependencies will be available.
if not os.environ.get('UV'):
    from pathlib import Path
    crisp_dir = Path(__file__).parent.parent.absolute()
    os.execvp('uv', [
        'uv', 'run',
        '--project', str(crisp_dir),
        '--extra', 'graph',
        str(Path(__file__).absolute()),
    ] + sys.argv[1:])

import argparse
from collections import defaultdict
import json

from crisp.__main__ import parse_node_id_arg_and_check_tag
from crisp.config import Config
from crisp.history import get_history
from crisp.mvir import MVIR, FindUnsafe2AnalysisNode
from crisp.workflow import Workflow

def parse_args():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', '-c', dest='config_path', default='crisp.toml')
    ap.add_argument('--mvir-storage-dir')
    ap.add_argument('--reflog-tag')
    ap.add_argument('node', nargs='?', default='current')
    return ap.parse_args()

def get_state(w, tree):
    dct = {}
    json_tree = w.find_unsafe2_json(tree)
    for path, file_node in json_tree.files.items():
        file_node = w.mvir.node(file_node)
        dct[path] = file_node.body_json()
    return dct

def diff_state(old, new):
    diff = {}
    all_keys = set(old.keys()) | set(new.keys())
    for k in all_keys:
        old_v = old.get(k)
        new_v = new.get(k)

        if old_v is None and new_v is None:
            continue

        ty = type(old_v) if old_v is not None else type(new_v)
        if issubclass(ty, int):
            d = (new_v or 0) - (old_v or 0)
            if d != 0:
                diff[k] = d
        elif issubclass(ty, str):
            if k == 'filename' and old_v == new_v:
                diff[k] = old_v
            else:
                if old_v != new_v:
                    diff[k] = (old_v, new_v)
        elif issubclass(ty, dict):
            d = diff_state(old_v or {}, new_v or {})
            is_empty = len(d) == 0 or (len(d) == 1 and 'filename' in d)
            if not is_empty:
                diff[k] = d
        else:
            raise TypeError(f'unsupported types: {type(old_v)}, {type(new_v)}')

    return diff

def iter_metrics(dct):
    """
    Iterate over the individual metrics in a `FunctionOutputs` or `TypeOutputs`
    JSON dict.
    """
    for k, v in dct.items():
        if k == 'total_unsafe':
            continue
        if isinstance(v, int):
            # Note `bool` is a subtype of `int`, so this case covers bools too
            yield v
        elif isinstance(v, dict):
            # Currently all maps in these types have integer values
            for kk, vv in v.items():
                assert isinstance(vv, int), f'non-integer metric at {k}.{kk}'
                yield vv
        elif isinstance(v, str):
            continue
        elif isinstance(v, (tuple, list)) and all(isinstance(x, (str, type(None))) for x in v):
            # A pair of `(old, new)` for a string-typed field.
            continue
        else:
            raise TypeError(f'unexpected value type {type(v)} for {k!r}')

def input_is_path(inp):
    """
    Returns `True` if `inp` looks like a path, rather than a MVIR node ID or
    tag.
    """
    return '.' in inp or '/' in inp or '\\' in inp

def main():
    args = parse_args()

    if input_is_path(args.node):
        json_path = args.node
        with open(json_path) as f:
            j = json.load(f)
        init_state = j['init']
        diffs = j['diffs']
        print(f'read state diffs from {json_path}')

    else:
        cfg_kwargs = {}
        if args.mvir_storage_dir is not None:
            cfg_kwargs['mvir_storage_dir'] = os.path.abspath(args.mvir_storage_dir)
        cfg = Config.from_toml_file(args.config_path, **cfg_kwargs)

        mvir = MVIR(cfg.mvir_storage_dir, '.')
        w = Workflow(cfg, mvir)

        (node_id, is_tag) = parse_node_id_arg_and_check_tag(mvir, args.node)
        node = mvir.node(node_id)

        history = get_history(mvir, node)
        print(f'history: {len(history)} entries')

        assert len(history) > 0

        trees = list(tree for tree, op in reversed(history))

        init_state = get_state(w, trees[0])

        diffs = []
        prev_state = init_state
        for tree in trees[1:]:
            state = get_state(w, tree)
            diff = diff_state(prev_state, state)
            diffs.append(diff)
            prev_state = state

        json_path = f'history-progress-{str(node_id)[:12]}.json'
        with open(json_path, 'w') as f:
            j = {
                'init': init_state,
                'diffs': diffs,
                'trees': [str(tree.node_id()) for tree in trees],
            }
            json.dump(j, f)
        print(f'wrote state diffs to {json_path}')


    # We now count up the number of diffs that have various properties

    # Overall `total_unsafe` decreased/increased/was unchanged
    count_total_unsafe_down = 0
    count_total_unsafe_up = 0
    count_total_unsafe_same = 0

    # Any metric decreased/increased
    count_any_metric_down = 0
    count_any_metric_up = 0

    # All metrics were unchanged
    count_all_metrics_same = 0

    # Modified at least one function/file that was modified in the previous
    # diff
    count_touched_same_function = 0
    count_touched_same_file = 0


    prev_touched_functions = set()
    prev_touched_files = set()
    for diff in diffs:
        # Each `diff[file_path]` is a find-unsafe2 `Outputs` object.
        total_unsafe = sum(x.get('total_unsafe', 0) for x in diff.values())
        if total_unsafe < 0:
            count_total_unsafe_down += 1
        elif total_unsafe > 0:
            count_total_unsafe_up += 1
        else:
            count_total_unsafe_same += 1

        touched_functions = set()
        touched_files = set()

        any_down = False
        any_up = False

        for file_name, diff_file in diff.items():
            all_items = list(diff_file.get('fns', {}).items()) \
                    + list(diff_file.get('types', {}).items())
            for item_name, diff_item in all_items:
                item_any_down = any(m < 0 for m in iter_metrics(diff_item))
                item_any_up = any(m > 0 for m in iter_metrics(diff_item))
                if item_any_down or item_any_up:
                    touched_functions.add(item_name)
                    touched_file = diff_item['filename']
                    if isinstance(touched_file, (list, tuple)):
                        touched_file = touched_file[-1]
                    touched_files.add(touched_file)

                is_ffi_function = init_state[file_name]['fns'] \
                        .get(item_name, {}).get('ffi_symbol') is not None
                if not is_ffi_function:
                    any_down |= item_any_down
                    any_up |= item_any_up

        if any_down:
            count_any_metric_down += 1
        if any_up:
            count_any_metric_up += 1
        if not any_down and not any_up:
            count_all_metrics_same += 1

        if len(prev_touched_functions & touched_functions) > 0:
            count_touched_same_function += 1
        if len(prev_touched_files & touched_files) > 0:
            count_touched_same_file += 1
        prev_touched_functions = touched_functions
        prev_touched_files = touched_files

    def show_count(val, desc, is_delta = False):
        denom = len(diffs)
        if is_delta:
            denom -= 1
        pct = val / denom * 100
        print(f'{val:6}  {pct:5.1f}%  {desc}')

    show_count(count_total_unsafe_down, 'Overall total_unsafe decreased')
    show_count(count_total_unsafe_up, 'Overall total_unsafe increased')
    show_count(count_total_unsafe_same, 'Overall total_unsafe was unchanged')

    show_count(count_any_metric_down, 'At least one metric decreased')
    show_count(count_any_metric_up, 'At least one metric increased')
    show_count(count_all_metrics_same, 'All metrics were unchanged')

    show_count(count_touched_same_function, 'Touched a function touched by the previous step')
    show_count(count_touched_same_file, 'Touched a file touched by the previous step')

if __name__ == '__main__':
    main()
