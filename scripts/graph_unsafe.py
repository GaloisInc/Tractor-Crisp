"""
Graph unsafe count over time throughout the history of a refactoring run.

Usage:
    uv run --project /path/to/tractor-crisp --extra graph \
        /path/to/tractor-crisp/scripts/graph_unsafe.py \
        INPUTS...

`INPUTS` can be any combination of MVIR tags (such as `current`, the default),
node IDs, and paths to files containing unsafe count data.  Running on an MVIR
tag or node ID will write the counts to a file for future use, in addition to
producing the graph.

The graph is always written to `./graph.png`.
"""

import argparse
from datetime import timedelta
import itertools
import os
import sys
import toml

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter

from crisp.__main__ import parse_node_id_arg_and_check_tag
from crisp.config import Config
from crisp.history import get_history
from crisp.mvir import MVIR, FindUnsafe2AnalysisNode

def parse_args():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', '-c', dest='config_path', default='crisp.toml')
    ap.add_argument('--mvir-storage-dir')
    ap.add_argument('--reflog-tag')
    ap.add_argument('--print-interval', metavar='N', type=int, default=3600,
        help='print unsafe counts at intervals of N seconds')
    ap.add_argument('inputs', nargs='*', default=['current'],
        help='node IDs, tags, or TOML files containing lists of points')
    return ap.parse_args()

def get_points_mvir(args, cfg, mvir, node_id_arg):
    (node_id, is_tag) = parse_node_id_arg_and_check_tag(mvir, node_id_arg)
    node = mvir.node(node_id)

    history = get_history(mvir, node)
    print(f'history: {len(history)} entries')

    timestamp_map = {}
    reflog_tag = args.reflog_tag or (node_id_arg if is_tag else 'current')
    print(f'reading reflog of {reflog_tag!r}')
    for re in mvir.tag_reflog(reflog_tag):
        timestamp_map[re.node_id] = re.timestamp

    points = []
    for (n, _) in history:
        # Find the unsafe counts for `n`
        unsafe_op = None
        for ie in mvir.index(n.node_id()):
            if ie.kind != FindUnsafe2AnalysisNode.KIND:
                continue
            if ie.key != 'code':
                continue
            unsafe_op = mvir.node(ie.node_id)
            break
        if unsafe_op is None:
            continue

        # Get the reflog timestamp for `n`
        timestamp = timestamp_map.get(n.node_id())
        if timestamp is None:
            continue

        unsafe_json = mvir.node(unsafe_op.unsafe_json)
        unsafe_count = 0
        for file_node_id in unsafe_json.files.values():
            file_node = mvir.node(file_node_id)
            j = file_node.body_json()
            unsafe_count += j['total_unsafe']

        points.append((timestamp, unsafe_count))

    # Points currently follow history order, which is newest first.
    points.reverse()

    print(f'points: {len(points)} entries')
    for x in points:
        print(x)

    # We use TOML for this rather than JSON or CBOR because it has built-in
    # support for serializing `datetime` objects.
    toml_path = f'graph-unsafe-{str(node_id)[:12]}.toml'
    with open(toml_path, 'w') as f:
        toml.dump({'points': [{'t': t, 'u': u} for t,u in points]}, f)
    print(f'wrote points to {toml_path}')

    return node_id, is_tag, points

def get_points_file(path):
    with open(path) as f:
        x = toml.load(f)
        return [(y['t'], y['u']) for y in x['points']]

def input_is_path(inp):
    """
    Returns `True` if `inp` looks like a path, rather than a MVIR node ID or
    tag.
    """
    return '.' in inp or '/' in inp or '\\' in inp

def main():
    args = parse_args()

    needs_mvir = any(not input_is_path(inp) for inp in args.inputs)

    if needs_mvir:
        cfg_kwargs = {}
        if args.mvir_storage_dir is not None:
            cfg_kwargs['mvir_storage_dir'] = os.path.abspath(args.mvir_storage_dir)
        cfg = Config.from_toml_file(args.config_path, **cfg_kwargs)

        mvir = MVIR(cfg.mvir_storage_dir, '.')

    legend_labels = []
    points_lists = []
    for inp in args.inputs:
        if input_is_path(inp):
            name = os.path.splitext(os.path.basename(inp))[0]
            legend_labels.append(name)
            points_lists.append(get_points_file(inp))
        else:
            node_id, is_tag, points = get_points_mvir(args, cfg, mvir, inp)
            if is_tag:
                legend_labels.append(f'tag:{inp}')
            else:
                legend_labels.append(f'node:{str(node_id)[:7]}')
            points_lists.append(points)


    def format_func(x, pos):
        hours = int(x // 3600)
        minutes = int((x % 3600) // 60)
        seconds = int(x % 60)
        return f'{hours}:{minutes:02}'


    # Print summary stats
    for x in itertools.count(step = args.print_interval):
        print(f'\nat {format_func(x, None)}:')
        for (name, points) in zip(legend_labels, points_lists):
            base_date = points[0][0]
            y = min(yy for xx, yy in points if (xx - base_date).total_seconds() <= x)
            print(f'  {y:7}  {name}')
        all_ended = all((xx - points[0][0]).total_seconds() <= x
            for points in points_lists
            for xx, yy in points)
        if all_ended:
            break


    fig, ax = plt.subplots(figsize=(8, 5))
    for (name, points) in zip(legend_labels, points_lists):
        # Use elapsed time (in seconds) rather than an absolute `datetime` for
        # the X axis, so we can compare results from different runs.
        base_date = points[0][0]
        ax.plot(
            [(x[0] - base_date).total_seconds() for x in points],
            [x[1] for x in points],
            linestyle='-',
            label=name,
        )

    ax.grid(True)
    ax.set_xlabel("Elapsed time (HH:MM)")
    ax.xaxis.set_major_formatter(FuncFormatter(format_func))
    ax.set_ylabel("Unsafe operations")
    ax.legend()

    plt.savefig('graph.png')


if __name__ == '__main__':
    main()
