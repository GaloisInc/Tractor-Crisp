from cbor import cbor
from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
import stat
import tempfile
from typing import Any, ClassVar
from weakref import WeakValueDictionary


@dataclass(frozen=True)
class NodeId:
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


@dataclass(frozen=True)
class ReflogEntry:
    node_id: NodeId
    timestamp: datetime
    reason: Any


def datetime_to_serializable(dt):
    return [dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond]

def datetime_from_serializable(x):
    assert len(x) == 7
    return datetime(*x)


class MVIR:
    def __init__(self, path, src_dir):
        self._path = path
        self._src_dir = os.path.realpath(src_dir)
        # Maps `NodeId` to `Node`
        self._nodes = WeakValueDictionary()

    def _node_path(self, node_id):
        first, rest = node_id.raw[:1].hex(), node_id.raw[1:].hex()
        return os.path.join(self._path, 'nodes', first, rest)

    def node(self, node_id):
        return Node._get(self, node_id)

    def _tag_path(self, name):
        return os.path.join(self._path, 'tags', name)

    def set_tag(self, name, node_id, reason=None):
        if isinstance(node_id, Node):
            node_id = node_id.node_id()

        path = self._tag_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'ab') as f:
            timestamp = datetime.now()
            raw_timestamp = datetime_to_serializable(timestamp)
            cbor.dump([raw_timestamp, reason], f)
            f.write(node_id.raw)

    def tag(self, name):
        path = self._tag_path(name)
        with open(path, 'rb') as f:
            f.seek(-NodeId.LENGTH, os.SEEK_END)
            raw = f.read(NodeId.LENGTH)
            return NodeId(raw)

    def tag_reflog(self, name):
        path = self._tag_path(name)
        reflog = []
        size = os.stat(path).st_size
        with open(path, 'rb') as f:
            while f.tell() < size:
                raw_timestamp, reason = cbor.load(f)
                timestamp = datetime_from_serializable(raw_timestamp)
                raw_node_id = f.read(NodeId.LENGTH)
                node_id = NodeId(raw_node_id)
                reflog.append(ReflogEntry(node_id, timestamp, reason))
        return reflog


class Node:
    def __init__(self, mvir, node_id, metadata, body_offset):
        self.__class__._check_metadata(metadata)
        self._mvir = mvir
        self._node_id = node_id
        self._metadata = metadata
        self._body_offset = body_offset
        self._body = None

    EXPECTED_METADATA_KEYS = {'kind'}

    @classmethod
    def _check_metadata(cls, metadata):
        if metadata.keys() == cls.EXPECTED_METADATA_KEYS:
            return True
        missing = cls.EXPECTED_METADATA_KEYS - metadata.keys()
        unexpected = metadata.keys() - cls.EXPECTED_METADATA_KEYS
        if missing and extra:
            raise ValueError('missing keys %r and unexpected keys %r for %s' %
                (missing, unexpected, cls.__name__))
        elif missing:
            raise ValueError('missing keys %r for %s' % (missing, cls.__name__))
        else:
            assert unexpected
            raise ValueError('unexpected keys %r for %s' % (unexpected, cls.__name__))

    @classmethod
    def new(cls, mvir, body=b'', **metadata):
        assert 'kind' not in metadata
        metadata['kind'] = cls.KIND
        return cls._create(mvir, metadata, body)

    @staticmethod
    def _create(mvir, metadata, body):
        meta_bytes = cbor.dumps(metadata)
        h = hashlib.sha256()
        h.update(meta_bytes)
        h.update(body)
        node_id = NodeId(h.digest())

        body_offset = len(meta_bytes)

        def populate(n):
            if n._metadata is None:
                n._metadata = metadata
            if n._body_offset is None:
                n._body_offset = body_offset
            if n._body is None:
                n._body = body

        if node_id in mvir._nodes:
            n = mvir._nodes[node_id]
            # If some parts haven't been loaded from disk yet, populate them
            # with the values we hav eavailable.
            populate(n)
            return n

        path = mvir._node_path(node_id)
        cls = NODE_KIND_MAP[metadata['kind']]
        n = cls(mvir, node_id, metadata, body_offset)
        populate(n)
        if os.path.exists(path):
            # No need to write if the file already exists.
            mvir._nodes[node_id] = n
            return n

        dir_path = os.path.dirname(path)
        os.makedirs(dir_path, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_path)

        try:
            with os.fdopen(tmp_fd, 'wb') as f:
                f.write(meta_bytes)
                assert f.tell() == n._body_offset
                f.write(body)
            # chmod 444
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            os.rename(tmp_path, path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                os.unlink(tmp_path)

        # Update `_nodes` only once we know the write to disk has succeeded.
        mvir._nodes[node_id] = n
        return n

    @staticmethod
    def _get(mvir, node_id):
        if node_id in mvir._nodes:
            return mvir._nodes[node_id]
        else:
            path = mvir._node_path(node_id)
            with open(path, 'rb') as f:
                metadata = cbor.load(f)
                body_offset = f.tell()
            cls = NODE_KIND_MAP[metadata['kind']]
            n = cls(mvir, node_id, metadata, body_offset)
            mvir._nodes[node_id] = n
            return n

    def _load_body(self):
        path = self._mvir._node_path(self._node_id)
        with open(path, 'rb') as f:
            f.seek(self._body_offset)
            self._body = f.read()

    def node_id(self):
        return self._node_id

    def metadata(self):
        return self._metadata

    def body(self):
        if self._body is None:
            self._load_body()
        return self._body

class FileNode(Node):
    KIND = 'file'

class TreeNode(Node):
    KIND = 'tree'
    EXPECTED_METADATA_KEYS = Node.EXPECTED_METADATA_KEYS.union({'files'})

    @classmethod
    def _check_metadata(cls, metadata):
        super()._check_metadata(metadata)

        files = metadata['files']
        if not isinstance(files, dict):
            raise TypeError('metadata entry `files` must be a dict')
        for k,v in files.items():
            if not isinstance(k, str):
                raise TypeError('`files` keys must be str, but got %r (%r)' % (k, type(k)))
            if not isinstance(v, NodeId):
                raise TypeError('`files` values must be NodeId, but got %r (%r)' % (v, type(v)))

    files = property(lambda self: self._metadata['files'])

NODE_CLASSES = [
    FileNode,
    TreeNode,
]

def _build_node_kind_map(classes):
    m = {}
    for cls in classes:
        assert cls.KIND not in m
        m[cls.KIND] = cls
    return m
NODE_KIND_MAP = _build_node_kind_map(NODE_CLASSES)
