import argparse
from datetime import timedelta
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

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
    ap.add_argument('node', nargs='?', default='current')
    return ap.parse_args()

def main():
    args = parse_args()

    cfg_kwargs = {}
    if args.mvir_storage_dir is not None:
        cfg_kwargs['mvir_storage_dir'] = os.path.abspath(args.mvir_storage_dir)
    cfg = Config.from_toml_file(args.config_path, **cfg_kwargs)

    mvir = MVIR(cfg.mvir_storage_dir, '.')
    (node_id, is_tag) = parse_node_id_arg_and_check_tag(mvir, args.node)
    node = mvir.node(node_id)

    history = get_history(mvir, node)
    print(f'history: {len(history)} entries')

    timestamp_map = {}
    reflog_tag = args.reflog_tag or (args.node if is_tag else 'current')
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

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot([x[0] for x in points], [x[1] for x in points], color='b', linestyle='-')
    ax.grid(True)
    ax.set_ylabel("Unsafe operations")

    date_form = mdates.DateFormatter("%m-%d")
    ax.xaxis.set_major_formatter(date_form)

    # Draw vertical lines at 24 hours and 48 hours
    #ax.axvline(x=points[0][0] + timedelta(hours = 24), linestyle='--', linewidth=1)
    #ax.axvline(x=points[0][0] + timedelta(hours = 48), linestyle='--', linewidth=1)

    plt.savefig('graph.png')

if __name__ == '__main__':
    main()
