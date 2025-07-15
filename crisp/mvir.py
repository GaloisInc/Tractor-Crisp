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

    def new_node(self, metadata, body=b''):
        return Node._create(self, metadata, body)

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
    def __init__(self, mvir, name, path):
        self._mvir = mvir
        self._name = name
        self._path = path
        self._metadata = None
        self._body_offset = None
        self._body = None

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
        n = Node(mvir, node_id, path)
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
            n = Node(mvir, node_id, path)
            mvir._nodes[node_id] = n
            return n

    def _load_metadata(self):
        with open(self._path, 'rb') as f:
            self._load_metadata_from(f)

    def _load_metadata_from(self, f):
        self._metadata = cbor.load(f)
        self._body_offset = f.tell()

    def _load_body(self):
        with open(self._path, 'rb') as f:
            if self._body_offset is None:
                self._load_metadata_from()
            else:
                f.seek(self._body_offset)
            self._body = f.read()

    def node_id(self):
        return self._name

    def metadata(self):
        if self._metadata is None:
            self._load_metadata()
        return self._metadata

    def body(self):
        if self._body is None:
            self._load_body()
        return self._body
