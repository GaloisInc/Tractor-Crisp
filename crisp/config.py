from dataclasses import dataclass
import os
import toml
import typing

class ConfigBase:
    @classmethod
    def from_dict(cls, d, config_path, **kwargs):
        d.update(kwargs)
        field_tys = typing.get_type_hints(cls)
        for k, v in d.items():
            if issubclass(field_tys[k], ConfigBase):
                d[k] = field_tys[k].from_dict(v, config_path)
        return cls(config_path=config_path, **d)

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

    src_globs: list[str]
    test_command: str
    base_dir: str = '.'
    mvir_storage_dir: str = 'crisp-storage'

    def __post_init__(self):
        config_dir = os.path.dirname(self.config_path)
        object.__setattr__(self, 'base_dir', os.path.join(config_dir, self.base_dir))
        object.__setattr__(self, 'mvir_storage_dir',
            os.path.join(config_dir, self.mvir_storage_dir))

    def relative_path(self, path):
        """Convert `path` to a relative path based on `self.base_dir`."""
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

    def __post_init__(self):
        config_dir = os.path.dirname(self.config_path)
        object.__setattr__(self, 'cmake_src_dir',
            os.path.join(config_dir, self.cmake_src_dir))
        object.__setattr__(self, 'output_dir',
            os.path.join(config_dir, self.output_dir))
