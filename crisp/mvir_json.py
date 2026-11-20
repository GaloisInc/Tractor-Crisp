"""Editable JSON metadata and a binary body file for a single MVIR node.

Strings containing ``!`` are escaped with a leading ``!``, including object
keys.  This reserves tagged strings for scalar types and single-key objects
such as ``{"!tuple": [...]}`` for structured types.
"""

import base64
from dataclasses import fields
from datetime import datetime
import json
import math
from pathlib import Path
import typing

from .mvir import MVIR, NODE_KIND_MAP, Node, check_type
from .node_id import NodeId
from .planning import StepGoal, StepPlan


# Only these named types may be constructed from an exported document.
RECORD_TYPES = {cls.__name__: cls for cls in (StepPlan, StepGoal)}


def to_json(value):
    """Convert metadata to JSON values, preserving types with compact tags."""
    if isinstance(value, str):
        return '!' + value if '!' in value else value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else 'f!' + str(value)
    if isinstance(value, NodeId):
        return 'n!' + str(value)
    if isinstance(value, bytes):
        return 'b64!' + base64.b64encode(value).decode('ascii')
    if isinstance(value, datetime):
        return 'dt!' + value.isoformat()
    if isinstance(value, list):
        return [to_json(v) for v in value]
    if isinstance(value, tuple):
        return {'!tuple': [to_json(v) for v in value]}
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            return {to_json(k): to_json(v) for k, v in value.items()}
        return {'!dict': [[to_json(k), to_json(v)] for k, v in value.items()]}
    if type(value) in RECORD_TYPES.values():
        return {'!' + type(value).__name__: {
            f.name: to_json(getattr(value, f.name)) for f in fields(value)}}
    raise TypeError(f'unsupported metadata type: {type(value)}')


def _dict_from_pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError(f'duplicate metadata key: {k!r}')
        result[k] = v
    return result


def from_json(value):
    """Decode tagged JSON values; reject unrecognized or malformed tags."""
    if isinstance(value, str):
        if value.startswith('!'):
            return value[1:]
        if '!' not in value:
            return value
        tag, payload = value.split('!', 1)
        if tag == 'n':
            return NodeId.from_str(payload)
        if tag == 'b64':
            return base64.b64decode(payload, validate=True)
        if tag == 'dt':
            return datetime.fromisoformat(payload)
        if tag == 'f' and payload in ('nan', 'inf', '-inf'):
            return float(payload)
        raise ValueError(f'unknown metadata tag in {value!r}; '
            'prefix literal strings containing "!" with "!"')
    if isinstance(value, list):
        return [from_json(v) for v in value]
    if isinstance(value, dict):
        # Escaped literal keys contain another ! after the escape prefix.
        tags = [k for k in value if k.startswith('!') and '!' not in k[1:]]
        if not tags:
            # Object keys are always strings.  Typed keys use !dict instead.
            pairs = [(from_json(k), from_json(v)) for k, v in value.items()]
            if any(not isinstance(k, str) for k, _ in pairs):
                raise ValueError('typed metadata keys must use a !dict object')
            return _dict_from_pairs(pairs)
        if len(value) != 1:
            raise ValueError('a tagged metadata object must have exactly one key')
        tag, = tags
        payload = value[tag]
        if tag == '!tuple' and isinstance(payload, list):
            return tuple(from_json(v) for v in payload)
        if tag == '!dict' and isinstance(payload, list):
            if any(not isinstance(p, list) or len(p) != 2 for p in payload):
                raise ValueError('!dict requires a list of key/value pairs')
            return _dict_from_pairs((from_json(k), from_json(v)) for k, v in payload)
        cls = RECORD_TYPES.get(tag[1:])
        if cls is not None and isinstance(payload, dict):
            record = cls(**from_json(payload))
            # Dataclass constructors do not enforce their field annotations.
            for name, ty in typing.get_type_hints(cls).items():
                try:
                    check_type(ty, getattr(record, name))
                except (AssertionError, TypeError) as e:
                    raise ValueError(f'invalid {cls.__name__}.{name}: {e}') from e
            return record
        raise ValueError(f'unknown metadata object tag or invalid payload: {tag!r}')
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError(f'unsupported JSON value: {type(value)}')


def _reject_constant(value):
    raise ValueError(f'invalid JSON constant {value!r}; use an f! tag')


def export_node(node: Node, path):
    """Write metadata to ``path`` and the unmodified body to ``path.body``."""
    path = Path(path)
    metadata = json.dumps(to_json(node.metadata()), indent=2, sort_keys=True,
        ensure_ascii=False, allow_nan=False) + '\n'
    body = node.body()
    path.write_text(metadata, encoding='utf-8')
    Path(str(path) + '.body').write_bytes(body)


def import_node(mvir: MVIR, path) -> Node:
    """Read and validate an export, then store a node with its content hash."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding='utf-8'),
        object_pairs_hook=_dict_from_pairs, parse_constant=_reject_constant)
    metadata = from_json(raw)
    if not isinstance(metadata, dict):
        raise ValueError('node metadata must be an object')
    kind = metadata.pop('kind', None)
    if not isinstance(kind, str) or kind not in NODE_KIND_MAP:
        raise ValueError(f'unknown or missing node kind: {kind!r}')
    body = Path(str(path) + '.body').read_bytes()
    return NODE_KIND_MAP[kind].new(mvir, body=body, **metadata)
