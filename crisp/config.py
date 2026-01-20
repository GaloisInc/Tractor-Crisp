from dataclasses import dataclass, field
import os
import toml
import typing
from typing import Optional

def _is_config_type(ty):
    return isinstance(ty, type) and issubclass(ty, ConfigBase)

class ConfigBase:
    @classmethod
    def from_dict(cls, d, config_path, **kwargs):
        d.update(kwargs)
        field_tys = typing.get_type_hints(cls)
        for k, v in d.items():
            ty = field_tys[k]
            if _is_config_type(ty):
                d[k] = ty.from_dict(v, config_path)
            origin = typing.get_origin(ty)
            args = typing.get_args(ty)
            if origin is dict and _is_config_type(args[1]):
                d[k] = {kk: args[1].from_dict(vv, config_path)
                    for kk, vv in v.items()}
        if 'config_path' in field_tys and 'config_path' not in d:
            d['config_path'] = config_path
        return cls(**d)

    @classmethod
    def from_toml_file(cls, f, **kwargs):
        if isinstance(f, str):
            path = f
            f = open(f, 'r')
        else:
            path = f.name
        d = toml.load(f)
        return cls.from_dict(d, path, **kwargs)

@dataclass(frozen = True)
class Config(ConfigBase):
    config_path: str

    transpile: 'TranspileConfig'

    project_name: str
    src_globs: list[str]
    # Shell command to run to test generated code.  This should exit with code
    # 0 on success (tests passed) and nonzero on failure.  Relative paths in
    # this command are interpreted relative to `base_dir` (specifically, the
    # command is run in a sandbox directory where a subset of the files from
    # `base_dir` have been checked out).
    test_command: str
    base_dir: str = '.'
    mvir_storage_dir: str = 'crisp-storage'
    # `model = None` means call `/v1/models` and pick the first from the list.
    model: str | None = None

    models: dict[str, 'ModelConfig'] = field(default_factory=dict)

    def __post_init__(self):
        config_dir = os.path.dirname(self.config_path)
        object.__setattr__(self, 'base_dir', os.path.join(config_dir, self.base_dir))
        object.__setattr__(self, 'mvir_storage_dir',
            os.path.join(config_dir, self.mvir_storage_dir))
        if isinstance(self.src_globs, str):
            object.__setattr__(self, 'src_globs', [self.src_globs])

    def relative_path(self, path):
        """
        Convert `path` to a relative path based on `self.base_dir`.

        MVIR `TreeNode`s use paths relative to `base_dir`.  So if `base_dir` is
        `/foo`, we commit `/foo/bar/baz.txt` to build a tree, and we then check
        out the tree into a sandbox, the path of the file within the sandbox
        will be `bar/baz.txt`.  This method is useful for converting the
        outside path to the MVIR/inside path, such as when building commands:
        `relative_path('/foo/bar/baz.txt') == 'bar/baz.txt'`.
        """
        base_abs = os.path.abspath(self.base_dir)
        path_abs = os.path.abspath(path)
        assert os.path.commonpath((base_abs, path_abs)) == base_abs, \
                'path %r is outside project base directory %r' % (path_abs, base_abs)
        path_rel = os.path.relpath(path_abs, base_abs)
        assert not path_rel.startswith(os.pardir + os.sep)
        return path_rel

@dataclass(frozen = True)
class TranspileConfig(ConfigBase):
    config_path: str
    cmake_src_dir: str
    output_dir: str
    # Basename (without extension) of the compilation unit that contains the
    # `main` entry point, if the project produces a binary.  For example, if
    # `main` is defined in `driver.c`, this should be set to `driver`.
    bin_main: Optional[str] = None
    # If set, only this target will be built (via `make foo` or similar) when
    # generating `compile_commands.json`.  This means only files used in this
    # target will be included in the generated Rust.
    single_target: str | None = None

    def __post_init__(self):
        config_dir = os.path.dirname(self.config_path)
        object.__setattr__(self, 'cmake_src_dir',
            os.path.join(config_dir, self.cmake_src_dir))
        object.__setattr__(self, 'output_dir',
            os.path.join(config_dir, self.output_dir))

@dataclass(frozen = True)
class ModelConfig(ConfigBase):
    prefill: str = ''
    prefill_think: str = ''
