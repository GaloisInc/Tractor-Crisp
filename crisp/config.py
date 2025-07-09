from dataclasses import dataclass
import os
import toml

@dataclass(frozen = True)
class Config:
    config_path: str
    base_dir: str
    src_globs: list[str]
    test_command: str

    def __post_init__(self):
        config_dir = os.path.dirname(self.config_path)
        object.__setattr__(self, 'base_dir', os.path.join(config_dir, self.base_dir))

    @classmethod
    def from_dict(cls, d, config_path):
        return cls(config_path=config_path, **d)

    @classmethod
    def from_toml_file(cls, f):
        if isinstance(f, str):
            path = f
            f = open(f, 'r')
        else:
            path = f.name
        d = toml.load(f)
        return cls.from_dict(d, path)
