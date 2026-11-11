"""
MVIR `NodeId` type.  This is split out into its own module to break an import
cycle.
"""
from dataclasses import dataclass
import hashlib
from typing import ClassVar


@dataclass(frozen=True)
class NodeId:
    """
    MVIR node ID.  See `crisp.mvir` for more details.
    """
    raw: bytes

    LENGTH: ClassVar[int] = hashlib.sha256().digest_size

    def __post_init__(self):
        assert len(self.raw) == NodeId.LENGTH

    def __str__(self):
        return self.raw.hex()

    def __repr__(self):
        return 'NodeId(%s)' % self

    @staticmethod
    def from_str(s):
        if len(s) != 2 * NodeId.LENGTH:
            raise ValueError('expected exactly %d characters' % (2 * NodeId.LENGTH))
        raw = bytes(int(s[i:i+2], 16) for i in range(0, len(s), 2))
        return NodeId(raw)

    def to_cbor(self):
        return self.raw

    @classmethod
    def from_cbor(cls, raw):
        return cls(raw)

    @classmethod
    def check_type(cls, x):
        assert isinstance(x, cls), 'expected %r, but got %r' % (cls, type(x))

