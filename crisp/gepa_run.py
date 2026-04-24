import argparse
import os

from .config import Config
from .mvir import MVIR
from .workflow import Workflow
from .__main__ import parse_node_id_arg


def parse_args():
    ap = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', '-c', dest='config_path', default='crisp.toml')
    ap.add_argument('--mvir-storage-dir')
    ap.add_argument('--c-code', default='c_code')
    ap.add_argument('node', nargs='?', default='current')
    return ap.parse_args()


def do_gepa(args, cfg):
    mvir = MVIR(cfg.mvir_storage_dir, '.')
    w = Workflow(cfg, mvir)

    c_code_node_id = parse_node_id_arg(mvir, args.c_code)
    n_c_code = mvir.node(c_code_node_id)

    code_node_id = parse_node_id_arg(mvir, args.node)
    n_code = mvir.node(code_node_id)

    # run_gepa(args, cfg, mvir, w, n_code, n_c_code) #TODO gepa here
    print()


def main():
    args = parse_args()

    cfg_kwargs = {}
    if args.mvir_storage_dir is not None:
        cfg_kwargs['mvir_storage_dir'] = os.path.abspath(args.mvir_storage_dir)
    cfg = Config.from_toml_file(args.config_path, **cfg_kwargs)

    do_gepa(args, cfg)


if __name__ == '__main__':
    main()
