"""Plan validation and the small progress window persisted between safety runs."""

import json
import math
import re

from .error import CrispError
from .mvir import SafetyProgressNode


PROGRESS_TAG = 'safety_progress'


def planning_interval(unsafe_count: int) -> int:
    """Log2 rounded up, in whole attempts, with one at the endgame."""
    return max(1, math.ceil(math.log2(max(1, unsafe_count))))


def load_progress(mvir, code, plans):
    if plans is None or not mvir.has_tag(PROGRESS_TAG):
        return None
    node = mvir.node(mvir.tag(PROGRESS_TAG))
    if node.code != code.node_id() or node.plans != plans.node_id():
        # An external plan install or code checkout starts a new window.
        return None
    return node.body_json()


def save_progress(mvir, code, plans, progress):
    node = SafetyProgressNode.new(mvir, code=code.node_id(),
        plans=plans.node_id(), body=json.dumps(progress, indent=2))
    mvir.set_tag(PROGRESS_TAG, node.node_id(), node.kind)


def validate_plan(old: str, new: str) -> None:
    headings = re.findall(r'^## .+$', new, re.MULTILINE)
    if headings != ['## FFI entry point rules', '## Conventions', '## Cluster guide']:
        raise CrispError('Planner must produce the three required plan sections')
    if not new.startswith('## FFI entry point rules\n'):
        raise CrispError('Planner put commentary before the plan')
    if old.split('## Conventions', 1)[0] != new.split('## Conventions', 1)[0]:
        raise CrispError('Planner changed the immutable FFI rules section')
    for heading in ('## Conventions', '## Cluster guide'):
        if not new.split(heading, 1)[1].split('\n## ', 1)[0].strip():
            raise CrispError(f'Planner left {heading} empty')
