from cbor import cbor
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
import stat
import tempfile
import typing
from typing import Any, ClassVar, Optional, Annotated, TypeVar
from types import NoneType
from weakref import WeakValueDictionary


T = TypeVar("T")

class MetadataTag:
    pass

Metadata = Annotated[T, MetadataTag]
"""
Mark a type as CRISP metadata.
"""

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

    def to_cbor(self):
        return self.raw

    @classmethod
    def from_cbor(cls, raw):
        return cls(raw)

    @classmethod
    def check_type(cls, x):
        assert isinstance(x, cls), 'expected %r, but got %r' % (cls, type(x))


def to_cbor(x):
    if isinstance(x, (NoneType, bool, int, float, str, bytes)):
        return x
    elif isinstance(x, (list, tuple)):
        return [to_cbor(y) for y in x]
    elif isinstance(x, dict):
        # Note: dict keys must be sortable.  Sorting ensures that the hash is
        # consistent even if the dict insertion order varies.
        return sorted(((to_cbor(k), to_cbor(v)) for k,v in x.items()), key=lambda x: x[0])
    elif isinstance(x, datetime):
        return (x.year, x.month, x.day, x.hour, x.minute, x.second, x.microsecond)
    else:
        return x.to_cbor()

def from_cbor(ty, x):
    origin = typing.get_origin(ty) or ty
    if origin in (NoneType, bool, int, float, str, bytes):
        assert isinstance(x, ty)
        return x
    elif origin is list:
        assert isinstance(x, (list, tuple))
        elem_ty, = typing.get_args(ty)
        return [from_cbor(elem_ty, y) for y in x]
    elif origin is tuple:
        assert isinstance(x, (list, tuple))
        elem_tys = typing.get_args(ty)
        assert len(elem_tys) == len(x)
        return tuple(from_cbor(t, y) for t, y in zip(elem_tys, x))
    elif origin is dict:
        # Dicts are serialized as lists of pairs.
        assert isinstance(x, (list, tuple))
        key_ty, value_ty = typing.get_args(ty)
        return {from_cbor(key_ty, k): from_cbor(value_ty, v) for k,v in x}
    elif origin is datetime:
        assert isinstance(x, (list, tuple))
        assert len(x) == 7
        return datetime(*x)
    elif origin is typing.Any:
        return x
    elif origin is typing.Union:
        for variant_ty in typing.get_args(ty):
            try:
                return from_cbor(variant_ty, x)
                return
            except (TypeError, AssertionError):
                pass
    else:
        return ty.from_cbor(x)

def check_type(ty, x):
    origin = typing.get_origin(ty) or ty
    if origin in (NoneType, bool, int, float, str, bytes):
        assert isinstance(x, ty), 'expected %r, but got %r: %r' % (origin, type(x), x)
    elif origin is list:
        assert isinstance(x, list), 'expected %r, but got %r: %r' % (origin, type(x), x)
        elem_ty, = typing.get_args(ty)
        for y in x:
            check_type(elem_ty, y)
    elif origin is tuple:
        assert isinstance(x, (list, tuple)), 'expected %r, but got %r: %r' % (origin, type(x), x)
        elem_tys = typing.get_args(ty)
        assert len(elem_tys) == len(x)
        for t, y in zip(elem_tys, x):
            check_type(t, y)
    elif origin is dict:
        assert isinstance(x, dict), 'expected %r, but got %r: %r' % (origin, type(x), x)
        key_ty, value_ty = typing.get_args(ty)
        for k, v in x.items():
            check_type(key_ty, k)
            check_type(value_ty, v)
    elif origin is typing.Union:
        for variant_ty in typing.get_args(ty):
            try:
                check_type(variant_ty, x)
                return
            except (TypeError, AssertionError):
                pass
    else:
        return ty.check_type(x)

def _metadata_field_types(cls: type) -> dict[str, type]:
    """
    Get all of the `Metadata[T]` field types of a type.
    """
    type_hints = typing.get_type_hints(cls, include_extras=True)
    field_types: dict[str, type] = {}
    for field_name, ty in type_hints.items():
        if typing.get_origin(ty) is typing.get_origin(Metadata):
            type_args = typing.get_args(ty)
            if len(type_args) != len(typing.get_args(Metadata)):
                continue
            base_ty, tag = type_args
            if tag is not MetadataTag:
                continue
            field_types[field_name] = base_ty
    return field_types

def _dataclass_to_cbor(x):
    '''Convert `x` to a CBOR-serializable form.  This is a default
    implementation for use in classes that have `dataclass`-style typed
    fields.'''
    cls = x.__class__
    field_tys = typing.get_type_hints(cls)
    values = tuple(getattr(x, name) for name in field_tys.keys())
    return to_cbor(values)

@classmethod
def _dataclass_from_cbor(cls, raw):
    field_tys = typing.get_type_hints(cls)
    expect_ty = tuple[*field_tys.values()]
    values = from_cbor(expect_ty, raw)
    return cls(*values)


@dataclass(frozen=True)
class ReflogEntry:
    node_id: NodeId
    timestamp: datetime
    reason: Any

    to_cbor = _dataclass_to_cbor
    from_cbor = _dataclass_from_cbor

@dataclass(frozen=True)
class IndexEntry:
    node_id: NodeId
    kind: str
    key: str

    to_cbor = _dataclass_to_cbor
    from_cbor = _dataclass_from_cbor


def _metadata_node_ids(x):
    if isinstance(x, (NoneType, bool, int, float, str, bytes, datetime)):
        return
    elif isinstance(x, NodeId):
        yield x
    elif isinstance(x, (list, tuple)):
        for y in x:
            yield from _metadata_node_ids(y)
    elif isinstance(x, dict):
        for k,v in x.items():
            yield from _metadata_node_ids(k)
            yield from _metadata_node_ids(v)
    else:
        raise TypeError('unsupported type in metadata: %r' % (type(x),))


class MVIR:
    def __init__(self, path, src_dir):
        self._path = path
        self._src_dir = os.path.realpath(src_dir)
        # Maps `NodeId` to `Node`
        self._nodes = WeakValueDictionary()
        self._stamp_mtime_cache = {}

    def _node_path(self, node_id):
        first, rest = node_id.raw[:1].hex(), node_id.raw[1:].hex()
        return os.path.join(self._path, 'nodes', first, rest)

    def node_ids_with_prefix(self, s):
        s = s.lower()
        first, rest = s[:2], s[2:]
        dir_path = os.path.join(self._path, 'nodes', first)
        try:
            file_names = os.listdir(dir_path)
        except OSError:
            return []
        return [NodeId.from_str(first + name) for name in file_names if name.startswith(rest)]

    def _nodes_newer_than(self, mtime):
        '''Yields `NodeId`s for all nodes whose file is newer than or equal to
        `mtime`.  If `mtime` is `None`, yields all `NodeId`s that exist on
        disk.'''
        base = os.path.join(self._path, 'nodes')
        for dir_name in os.listdir(base):
            if dir_name.startswith('.'):
                continue
            dir_path = os.path.join(base, dir_name)
            if mtime is not None:
                dir_mtime = os.stat(dir_path).st_mtime_ns
                if dir_mtime < mtime:
                    # Adding a new file to a directory updates the directory
                    # mtime.  Since we don't modify node files after creating
                    # them, the directory mtime should be close to the mtime of
                    # the newest file within.  So we exclude old directories on
                    # the assumption that they contain only old files.
                    #
                    # This is not strictly accurate: if writing the file takes
                    # a long time, then we could have a situation where
                    # `dir_mtime < mtime < file_mtime`, and the file is wrongly
                    # excluded from the `_nodes_newer_than(mtime)` output.
                    # However, in practice, the query `mtime` is always the
                    # mtime of some stamp file, and we don't update any stamp
                    # files (or take any other actions) between the time when
                    # we create the node file and when we finish writing to it.
                    continue

            for file_name in os.listdir(dir_path):
                if file_name.startswith('.'):
                    continue
                file_path = os.path.join(dir_path, file_name)
                if mtime is not None:
                    file_mtime = os.stat(file_path).st_mtime_ns
                    if file_mtime < mtime:
                        continue

                try:
                    yield NodeId.from_str(dir_name + file_name)
                except ValueError as e:
                    print('warning: unknown file %r in nodes directory (%s)' % (file_path, e))

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
            cbor.dump(to_cbor((timestamp, reason)), f)
            f.write(node_id.raw)

    def tag(self, name):
        path = self._tag_path(name)
        with open(path, 'rb') as f:
            f.seek(-NodeId.LENGTH, os.SEEK_END)
            raw = f.read(NodeId.LENGTH)
            return NodeId(raw)

    def has_tag(self, name):
        path = self._tag_path(name)
        return os.path.exists(path)

    def tag_reflog(self, name):
        path = self._tag_path(name)
        reflog = []
        size = os.stat(path).st_size
        with open(path, 'rb') as f:
            while f.tell() < size:
                timestamp, reason = from_cbor(tuple[datetime, Any], cbor.load(f))
                node_id = NodeId(f.read(NodeId.LENGTH))
                reflog.append(ReflogEntry(node_id, timestamp, reason))
        return reflog

    def _stamp_path(self, name):
        return os.path.join(self._path, 'stamps', name)

    def _stamp_mtime(self, name):
        if name in self._stamp_mtime_cache:
            return self._stamp_mtime_cache[name]

        path = self._stamp_path(name)
        if os.path.exists(path):
            mtime = os.stat(path).st_mtime_ns
        else:
            mtime = None
        self._stamp_mtime_cache[name] = mtime
        return mtime

    def _touch_stamp(self, name, content=b''):
        self._stamp_mtime_cache.pop(name, None)
        path = self._stamp_path(name)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            if len(content) > 0:
                f.write(content)

    def _read_stamp(self, name):
        path = self._stamp_path(name)
        if not os.path.exists(path):
            return b''
        with open(path, 'rb') as f:
            return f.read()

    # Index update logic
    #
    # Note on timestamps: we use mtime comparisons extensively to determine
    # which parts of the index need updates, but we assume only that the clock
    # never goes backwards, not that it goes forward between operations.  In
    # some cases on WSL it seems that two files written close together will get
    # identical mtimes down to the nanosecond.  Similar issues may occur on
    # filesystems with coarse-grained timestamps.  Thus, any time we see two
    # equal timestamps, we assume that either file may be newer than the other
    # (which usually means we must conservatively try to update the index).

    def _check_index(self):
        index_mtime = self._stamp_mtime('index')
        nodes_mtime = self._stamp_mtime('nodes')
        if index_mtime is None or (nodes_mtime is not None and nodes_mtime >= index_mtime):
            self._update_index(index_mtime)

    def _update_index(self, prev_mtime):
        # Load the set of nodes that were processed during the last index
        # update.
        prev_bytes = self._read_stamp('index')
        prev_nodes = set(NodeId(prev_bytes[i : i + NodeId.LENGTH])
            for i in range(0, len(prev_bytes), NodeId.LENGTH))

        processed_nodes = bytearray()
        for src_id in self._nodes_newer_than(prev_mtime):
            # `src_id` refers to a node with `mtime >= index_mtime`.  We record
            # all such nodes in `processed_nodes`, even if they were previously
            # processed.  This is important if the final `_touch_stamp` leaves
            # the `index_mtime` unchanged: in that case, the next index update
            # might see these same nodes again, and needs to know that they
            # were already processed (even though they were processed by the
            # previous update, not the current one).
            processed_nodes.extend(src_id.raw)

            if src_id in prev_nodes:
                # Node was already processed.
                continue

            n = self.node(src_id)
            for k, v in n.metadata().items():
                for dest_id in _metadata_node_ids(v):
                    entry = IndexEntry(src_id, n.kind, k)

                    path = self._index_path(dest_id)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, 'ab') as f:
                        cbor.dump(entry.to_cbor(), f)

        self._touch_stamp('index', processed_nodes)

    def _index_path(self, node_id):
        first, rest = node_id.raw[:1].hex(), node_id.raw[1:].hex()
        return os.path.join(self._path, 'index', first, rest)

    def index(self, node_id):
        '''Get a list of references to `node_id` from the index.  This will
        update the index first if needed.'''
        self._check_index()
        if isinstance(node_id, Node):
            node_id = node_id.node_id()
        path = self._index_path(node_id)
        try:
            f = open(path, 'rb')
        except FileNotFoundError:
            return ()
        with f:
            entries = []
            size = os.stat(path).st_size
            while f.tell() < size:
                entries.append(IndexEntry.from_cbor(cbor.load(f)))
        return entries


class Node:
    kind: Metadata[str]

    def __init__(self, mvir, node_id, metadata, body_offset):
        self.__class__._check_metadata(metadata)
        self._mvir = mvir
        self._node_id = node_id
        self._metadata = metadata
        self._body_offset = body_offset
        self._body = None
        self._body_json = None

    @classmethod
    def _check_metadata(cls, metadata):
        field_tys = _metadata_field_types(cls)

        if not isinstance(metadata, dict):
            raise TypeError('metadata should be a dict, but got %r (%r)' %
                (metadata, type(metadata)))
        if metadata.keys() != field_tys.keys():
            missing = field_tys.keys() - metadata.keys()
            unexpected = metadata.keys() - field_tys.keys()
            if missing and unexpected:
                raise ValueError('missing keys %r and unexpected keys %r for %s' %
                    (missing, unexpected, cls.__name__))
            elif missing:
                raise ValueError('missing keys %r for %s' % (missing, cls.__name__))
            else:
                assert unexpected
                raise ValueError('unexpected keys %r for %s' % (unexpected, cls.__name__))

        for k, v in metadata.items():
            try:
                check_type(field_tys[k], v)
            except (AssertionError, TypeError):
                print('error checking field %r' % k)
                raise

    @classmethod
    def new(cls, mvir, body=b'', **metadata):
        assert 'kind' not in metadata
        metadata['kind'] = cls.KIND
        cls._check_metadata(metadata)
        if isinstance(body, str):
            body = body.encode('utf-8')
        return cls._create(mvir, metadata, body)

    @staticmethod
    def _create(mvir, metadata, body):
        meta_bytes = cbor.dumps(to_cbor(metadata))
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

        # Bump the `nodes` timestamp file before writing the new node to disk.
        # If we did it the other way around, we might write the node but then
        # fail to update the timestamp, in which case the indexing code would
        # be unable to detect that a new node was created.
        mvir._touch_stamp('nodes')

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

    @classmethod
    def _metadata_from_cbor(cls, dct):
        field_tys = _metadata_field_types(cls)
        metadata = {}
        for name, value in dct.items():
            assert name not in metadata
            ty = field_tys[name]
            metadata[name] = from_cbor(ty, value)
        assert metadata.keys() == field_tys.keys()
        return metadata

    @classmethod
    def _get(cls, mvir, node_id):
        if node_id in mvir._nodes:
            return mvir._nodes[node_id]
        else:
            path = mvir._node_path(node_id)
            with open(path, 'rb') as f:
                metadata = cbor.load(f)
                body_offset = f.tell()
            metadata = {k: v for k,v in metadata}

            cls_name = metadata.get('kind')
            if cls_name is None:
                raise KeyError('missing `kind` in metadata')

            orig_cls_name = cls_name
            while (cls := NODE_KIND_MAP.get(cls_name)) is None:
                migration_func = NODE_MIGRATION_MAP.get(cls_name)
                if migration_func is None:
                    if cls_name == orig_cls_name:
                        raise KeyError(f'unknown node kind {cls_name!r}')
                    else:
                        raise KeyError(
                            f'unknown node kind {cls_name!r} (migrated from {orig_cls_name!r})')
                migration_func(metadata)

                new_cls_name = metadata.get('kind')
                if new_cls_name is None:
                    raise KeyError(
                        f'missing `kind` in metadata, after migration from {cls_name!r}')
                cls_name = new_cls_name

            metadata = cls._metadata_from_cbor(metadata)
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

    def read_raw_metadata(self):
        '''Get the raw CBOR value of the metadata, without running `from_cbor`
        on it.  Mainly useful for debugging metadata de/serialization.'''
        path = self._mvir._node_path(self._node_id)
        with open(path, 'rb') as f:
            return cbor.load(f)

    kind = property(lambda self: self.metadata()['kind'])

    def body(self):
        if self._body is None:
            self._load_body()
        return self._body

    def body_str(self):
        return self.body().decode('utf-8')

    def body_json(self):
        if self._body_json is None:
            self._body_json = json.loads(self.body().decode('utf-8'))
        return self._body_json

class FileNode(Node):
    KIND = 'file'

class TreeNode(Node):
    KIND = 'tree'
    files: Metadata[dict[str, NodeId]]

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

class CompileCommandsOpNode(Node):
    KIND = 'compile_commands_op_v2'
    c_code: Metadata[NodeId]
    cmds: Metadata[list[list[str]]]
    exit_code: Metadata[int]
    compile_commands: Metadata[Optional[NodeId]]

    c_code = property(lambda self: self._metadata['c_code'])
    cmds = property(lambda self: self._metadata['cmds'])
    exit_code = property(lambda self: self._metadata['exit_code'])
    compile_commands = property(lambda self: self._metadata['compile_commands'])

class TranspileOpNode(Node):
    KIND = 'transpile_op'
    compile_commands: Metadata[NodeId]
    c_code: Metadata[NodeId]
    cmd: Metadata[list[str]]
    exit_code: Metadata[int]
    rust_code: Metadata[Optional[NodeId]]

    compile_commands = property(lambda self: self._metadata['compile_commands'])
    c_code = property(lambda self: self._metadata['c_code'])
    cmd = property(lambda self: self._metadata['cmd'])
    exit_code = property(lambda self: self._metadata['exit_code'])
    rust_code = property(lambda self: self._metadata['rust_code'])

class SplitFfiOpNode(Node):
    KIND = 'split_ffi_op'
    old_code: Metadata[NodeId]
    new_code: Metadata[NodeId]
    # Commit hash of the `split_ffi_entry_points` version that was used
    # TODO: remove - no longer used.  Figure out a way to remove fields without
    # breaking the ability to read old MVIR storage
    commit: Metadata[str]
    # `body` stores the log output

    old_code = property(lambda self: self._metadata['old_code'])
    new_code = property(lambda self: self._metadata['new_code'])
    commit = property(lambda self: self._metadata['commit'])

class LlmOpNode(Node):
    KIND = 'llm_op'
    old_code: Metadata[NodeId]
    new_code: Metadata[NodeId]
    raw_prompt: Metadata[NodeId]
    request: Metadata[NodeId]
    response: Metadata[NodeId]

    @classmethod
    def _check_metadata(cls, metadata):
        super()._check_metadata(metadata)

        for k in ('old_code', 'new_code', 'raw_prompt', 'request', 'response'):
            if not isinstance(metadata[k], NodeId):
                raise TypeError('metadata entry `%s` must be a NodeId' % k)

    old_code = property(lambda self: self._metadata['old_code'])
    new_code = property(lambda self: self._metadata['new_code'])
    raw_prompt = property(lambda self: self._metadata['raw_prompt'])
    request = property(lambda self: self._metadata['request'])
    response = property(lambda self: self._metadata['response'])

class TestResultNode(Node):
    KIND = 'test_result_node'
    code: Metadata[NodeId]
    # `test_code` is a `TreeNode` containing additional code used only for
    # testing (e.g. the code for the test driver).
    test_code: Metadata[NodeId]
    cmd: Metadata[str]
    exit_code: Metadata[int]
    # `body` stores the test output

    code = property(lambda self: self._metadata['code'])
    test_code = property(lambda self: self._metadata['test_code'])
    cmd = property(lambda self: self._metadata['cmd'])
    exit_code = property(lambda self: self._metadata['exit_code'])

    @property
    def passed(self):
        return self.exit_code == 0

class CargoCheckJsonAnalysisNode(Node):
    KIND = 'cargo_check_json_analysis_node'
    code: Metadata[NodeId]
    exit_code: Metadata[int]
    json: Metadata[NodeId]
    # `body` stores the complete stdout and stderr logs

    code = property(lambda self: self._metadata['code'])
    exit_code = property(lambda self: self._metadata['exit_code'])
    json = property(lambda self: self._metadata['json'])

    @property
    def passed(self):
        return self.exit_code == 0

class InlineErrorsOpNode(Node):
    KIND = 'inline_errors_op_node'
    old_code: Metadata[NodeId]
    new_code: Metadata[NodeId]
    check_json: Metadata[NodeId]

    old_code = property(lambda self: self._metadata['old_code'])
    new_code = property(lambda self: self._metadata['new_code'])
    check_json = property(lambda self: self._metadata['check_json'])

class FindUnsafeAnalysisNode(Node):
    KIND = 'find_unsafe_analysis'
    code: Metadata[NodeId]
    # Commit hash of the `find_unsafe` version that was used
    commit: Metadata[str]
    stderr: Metadata[str]
    # `body` stores the JSON output

    code = property(lambda self: self._metadata['code'])
    commit = property(lambda self: self._metadata['commit'])
    stderr = property(lambda self: self._metadata['stderr'])

class EditOpNode(Node):
    KIND = 'edit_op'
    old_code: Metadata[NodeId]
    new_code: Metadata[NodeId]
    # `body` stores some kind of description of the edit.

    old_code = property(lambda self: self._metadata['old_code'])
    new_code = property(lambda self: self._metadata['new_code'])


class DefNode(Node):
    KIND = 'def'
    # `body` stores the source code of this def

class CrateNode(Node):
    KIND = 'crate'
    # Maps each def ID/path to a `DefNode`
    defs: Metadata[dict[str, NodeId]]

    defs = property(lambda self: self._metadata['defs'])

class SplitOpNode(Node):
    '''
    Split a `TreeNode` containing Rust code into a collection of separate
    `DefNode`s.
    '''
    KIND = 'split_op'
    cmd: Metadata[list[str]]
    exit_code: Metadata[int]
    # Input `TreeNode` (a collection of files)
    code_in: Metadata[NodeId]
    json_out: Metadata[NodeId]
    # Output `CrateNode` (a collection of defs)
    crate_out: Metadata[NodeId]

    cmd = property(lambda self: self._metadata['cmd'])
    exit_code = property(lambda self: self._metadata['exit_code'])
    code_in = property(lambda self: self._metadata['code_in'])
    json_out = property(lambda self: self._metadata['json_out'])
    crate_out = property(lambda self: self._metadata['crate_out'])


NODE_CLASSES = [
    FileNode,
    TreeNode,
    CompileCommandsOpNode,
    TranspileOpNode,
    SplitFfiOpNode,
    LlmOpNode,
    TestResultNode,
    CargoCheckJsonAnalysisNode,
    InlineErrorsOpNode,
    FindUnsafeAnalysisNode,
    EditOpNode,

    DefNode,
    CrateNode,
    SplitOpNode,
]

def _build_node_kind_map(classes):
    m = {}
    for cls in classes:
        assert cls.KIND not in m
        m[cls.KIND] = cls
    return m
NODE_KIND_MAP = _build_node_kind_map(NODE_CLASSES)


# Metadata migrations.  Whenever we make changes to an existing `Node`'s
# metadata types, we also change the `KIND` and add a migration function that
# maps the old metadata types to the new ones.  This allows newer versions of
# CRISP to load older nodes without error.

NODE_MIGRATION_MAP = {}

def migration(old_kind):
    def decorate(f):
        assert old_kind not in NODE_MIGRATION_MAP, f'duplicate migration for {old_kind!r}'
        NODE_MIGRATION_MAP[old_kind] = f
        return f
    return decorate

@migration('compile_commands_op')
def migrate_compile_commands_op(metadata):
    metadata['kind'] = 'compile_commands_op_v2'
    # cmd: list[str]  ->  cmds: list[list[str]]
    metadata['cmds'] = [metadata.pop('cmd')]
