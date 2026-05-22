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
            elif origin is list and _is_config_type(args[0]):
                d[k] = [args[0].from_dict(vv, config_path)
                    for vv in v]
        if 'config_path' in field_tys and 'config_path' not in d:
            d['config_path'] = config_path
        return cls(**d)

    @classmethod
    def from_toml_file(cls, f, **kwargs):
        if isinstance(f, str):
            path = f
            with open(f, 'r') as f_:
                d = toml.load(f_)
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
    #
    # If this is `None`, no tests will be run.  Operations that would normally
    # run tests will always report that the tests passed.
    test_command: str | None = None
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
        `self.relative_path('/foo/bar/baz.txt') == 'bar/baz.txt'`.
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
    output_dir: str
    artifacts: list[TranspileArtifactConfig]

    def __post_init__(self):
        config_dir = os.path.dirname(self.config_path)
        object.__setattr__(self, 'output_dir', os.path.join(config_dir, self.output_dir))
        # Check that all artifact names are distinct.
        seen = set()
        for a in self.artifacts:
            assert a.name not in seen, f'duplicate entry for artifact {a.name!r}'
            seen.add(a.name)

    def artifact(self, key: str | int | None) -> 'TranspileArtifactConfig':
        if key is None:
            assert len(self.artifacts) == 1, \
                    'must specify artifact name/index because config contains multiple artifacts'
            return self.artifacts[0]
        elif isinstance(key, int):
            return self.artifacts[key]
        else:
            for art in self.artifacts:
                if art.name == key:
                    return art
            raise KeyError(f'artifact {name!r} not found')

@dataclass(frozen = True)
class TranspileArtifactConfig(ConfigBase):
    config_path: str

    name: str
    configure_cmds: list[str] | str | None = None
    build_cmds: list[str] | str | None = None

    # Basename (without extension) of the compilation unit that contains the
    # `main` entry point, if this artifact is a binary.  For example, if `main`
    # is defined in `driver.c`, this should be set to `driver`.
    bin_main: Optional[str] = None

    # If set to the name of an artifact, `configure_cmds` and `build_cmds` are
    # ignored, and this artifact instead uses the same transpiled Rust code as
    # the named artifact.
    lib_from_bin_artifact: Optional[str] = None

    # When using Hayroll, this path is passed as the `--project-dir` option.
    # To minimize the number of additional layers of module hierarchy that
    # Hayroll introduces, this should be set to the innermost directory that
    # contains all of the relevant C sources.
    hayroll_project_dir: str = '.'

    # Generate a `build.rs` for this artifact that sets `rustc-link-lib=foo`
    # for each library in this list.
    system_libs: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.lib_from_bin_artifact is None:
            assert self.build_cmds is not None, \
                    f"artifact {self.name} is missing build_cmds"
            # Normalize `configure_cmds` and `build_cmds` to be `list[str]`,
            # using `object.__setattr__` to bypass `frozen = True`.
            if self.configure_cmds is None:
                object.__setattr__(self, 'configure_cmds', [])
            elif isinstance(self.configure_cmds, str):
                object.__setattr__(self, 'configure_cmds', [self.configure_cmds])
            if isinstance(self.build_cmds, str):
                object.__setattr__(self, 'build_cmds', [self.build_cmds])
        else:
            assert self.configure_cmds is None, \
                    f"artifact {self.name} must not have configure_cmds "\
                        'because lib_from_bin_artifact is set'
            assert self.build_cmds is None, \
                    f"artifact {self.name} must not have build_cmds "\
                        'because lib_from_bin_artifact is set'

        config_dir = os.path.dirname(self.config_path)
        object.__setattr__(self, 'hayroll_project_dir',
            os.path.join(config_dir, self.hayroll_project_dir))

@dataclass(frozen = True)
class ModelConfig(ConfigBase):
    prefill: str = ''
    prefill_think: str = ''
    # Which mode to use for embedding files into the LLM input/output.  The
    # options are listed in `llm_format` for options.
    #
    # This can be overridden by setting `$CRISP_LLM_FILE_FORMATTER`.  If the
    # env var is set, the `file_formatter_kwargs` will be ignored.
    file_formatter: str = 'xml'
    file_formatter_kwargs: dict = field(default_factory=dict)
