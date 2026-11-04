from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
import itertools
import re

from .error import ValidateErrorCollector
from .mvir import MVIR, NodeId


def _validate_filter(x: str | list[str] | None):
    if x is None:
        pass
    elif isinstance(x, str):
        _ = re.compile(f'^(?:{x})$')
    elif isinstance(x, list):
        pass
    else:
        raise TypeError(f'expected `str | list[str] | None`, but got {type(x)}')

def _compile_filter(x: str | list[str] | None) -> Callable[[str], bool]:
    if x is None:
        return lambda s: True
    elif isinstance(x, str):
        r = re.compile(f'^(?:{x})$')
        return lambda s: r.match(s) is not None
    elif isinstance(x, list):
        x = set(x)
        return lambda s: s in x
    else:
        raise TypeError(f'expected `str | list[str] | None`, but got {type(x)}')

def _iter_filter(x: str | list[str] | None, dct: dict):
    """
    Iterate over (k, v) pairs from `dct` that match the filter `x`.
    """
    if x is None:
        yield from dct.items()
    elif isinstance(x, list):
        for k in x:
            if k in dct:
                yield (k, dct[k])
    elif isinstance(x, str):
        r = re.compile(f'^(?:{x})$')
        for k, v in dct.items():
            if r.match(k) is not None:
                yield (k, v)
    else:
        raise TypeError(f'expected `str | list[str] | None`, but got {type(x)}')

@dataclass(frozen = True)
class StepGoal:
    # What kind of unsafety to count within each type or function.  A
    # two-element key like `("uses_foreign_fn", "printf")` can be used to count
    # uses of individual functions/fields/etc.  A single-element key referring
    # to a map like `"uses_foreign_fn"` adds up all elements of the map.
    key: str | tuple[str, str]
    # If the total unsafety count is <= max_count, then the goal is achieved.
    max_count: int = 0

    # Filters.  We count unsafe within items whose names match `self.functions`
    # or `self.types` (depending on item kind) and whose containing file name
    # matches `self.files`.  Each filter is either a regex, a list of literal
    # strings, or unset (matching all names).
    files: str | list[str] | None = None
    functions: str | list[str] | None = None
    types: str | list[str] | None = None

    def validate(self, ec: ValidateErrorCollector) -> bool:
        if self.max_count < 0:
            ec.err_msg('negative max_count')

        for k in ('files', 'functions', 'types'):
            with ec.context(lambda: f'in `{k}` filter'):
                try:
                    _validate_filter(getattr(self, k))
                except (TypeError, re.PatternError) as e:
                    ec.err(e)

    def _iter_matching_items(self, mvir: MVIR, unsafe_json: list[dict]):
        files_filter = _compile_filter(self.files)

        for j in unsafe_json:
            for fn_name, j_fn in _iter_filter(self.functions, j['fns']):
                if j_fn['ffi_symbol'] is not None:
                    continue
                if not files_filter(j_fn['filename']):
                    continue
                yield 'fn', fn_name, j_fn

            for ty_name, j_ty in _iter_filter(self.types, j['types']):
                if not files_filter(j_ty['filename']):
                    continue
                yield 'ty', ty_name, j_ty

    def _iter_matching_items_with_cost(self, mvir: MVIR, unsafe_json: list[dict]):
        if isinstance(self.key, str):
            key = self.key
            for kind, name, val in self._iter_matching_items(mvir, unsafe_json):
                # Note `bool` is a subclass of `int`, with value 0 or 1.
                x = val.get(key)
                if x is None:
                    continue
                if isinstance(x, dict):
                    x = sum(x.values())
                yield (kind, name, val, int(x))
        else:
            key1, key2 = self.key
            for kind, name, val in self._iter_matching_items(mvir, unsafe_json):
                m = val.get(key1)
                if m is None:
                    continue
                x = m.get(key2, 0)
                if x is None:
                    continue
                yield (kind, name, val, int(x))

    def _iter_matching_nonzero_items(self, mvir: MVIR, unsafe_json: list[dict]):
        """Yield `(kind, name, item_json)` for every item that contributes a
        nonzero amount toward `self.count()`."""
        for kind, name, val, cost in self._iter_matching_items_with_cost(mvir, unsafe_json):
            if cost > 0:
                yield (kind, name, val)

    def count(self, mvir: MVIR, unsafe_json: list[dict]) -> int:
        """Count the selected metric in matching implementation items."""
        return sum(cost for _, _, _, cost
                in self._iter_matching_items_with_cost(mvir, unsafe_json))

    def count_excess(self, mvir: MVIR, unsafe_json: list[dict]) -> int:
        """Return the count above the goal's limit, clamped at zero."""
        return max(0, self.count(mvir, unsafe_json) - self.max_count)

    def to_cbor(self):
        out = {
            'key': self.key if isinstance(self.key, str) else list(self.key),
            'max_count': self.max_count,
        }
        for name in ('files', 'functions', 'types'):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out

    @classmethod
    def from_cbor(cls, raw):
        key = raw['key']
        return cls(
            key = key if isinstance(key, str) else tuple(key),
            max_count = raw.get('max_count', 0),
            files = raw.get('files'),
            functions = raw.get('functions'),
            types = raw.get('types'),
        )

@dataclass
class StepPlan:
    # Natural-language description of this step.
    description: str
    # The step is complete once all of these goals are satisfied.
    goals: list[StepGoal] = field(default_factory = list)
    # Natural-language planning documents for the current step and its
    # ancestors, ordered from most general to most specific.  `details[-1]` is
    # the `SAFETY_PLAN.md` from the planning step that produced this
    # `StepPlan`.
    details: list[NodeId] = field(default_factory = list)
    # Number of times we've tried subdividing this step.
    subdiv_attempts: int = 0

    def count(self, mvir: MVIR, unsafe_json: list[dict]) -> int:
        return sum(goal.count(mvir, unsafe_json) for goal in self.goals)

    def count_excess(self, mvir: MVIR, unsafe_json: list[dict]) -> int:
        """Sum excess counts above each goal's limit; zero means all goals met."""
        return sum(goal.count_excess(mvir, unsafe_json) for goal in self.goals)

    def measure(self, mvir: MVIR, unsafe_json: list[dict]):
        """
        Measure the total size of all items that currently contribute to the
        safety count of at least one unmet goal.  For each unmet goal `g`,
        every item with a nonzero unsafety count for `g.key` will be included.
        However, each item is included at most once, even if it contributes to
        multiple goals.
        """
        out = {
            'functions': 0,
            'types': 0,
            'lines': 0,
            'functions_by_file': defaultdict(list),
            'types_by_file': defaultdict(list),
        }
        seen = set()
        for goal in self.goals:
            for kind, name, val in goal._iter_matching_nonzero_items(mvir, unsafe_json):
                filename = val['filename']
                key = (kind, name, filename)
                if key in seen:
                    continue
                seen.add(key)

                if kind == 'fn':
                    out['functions'] += 1
                    out['functions_by_file'][filename].append(name)
                elif kind == 'ty':
                    out['types'] += 1
                    out['types_by_file'][filename].append(name)
                else:
                    assert False, f'unexpected: {kind!r}'

                out['lines'] += val['size']

        return out

    def relevant_code(self, mvir: MVIR, unsafe_json: list[dict]) -> list[dict]:
        """
        Return a representation of up to 50 items that are relevant to this
        step's unmet goals, for rendering in a prompt template.

        Example output:
        ```
        [
            {
                'filename': 'foo.c',
                'items': [
                    {'name': 'f', 'metrics': 'total_unsafe = 3, uses_foreign_fn.printf = 1'},
                    {'name': 'g', 'metrics': 'total_unsafe = 1'},
                ],
                'other_count': 123,
            },
            {
                'filename': 'other.c',
                'items': [
                    {'name': 'h', 'metrics': 'total_unsafe = 2'},
                ],
                'other_count': 0,
            },
        ]
        ```

        `other_count` gives the number of items in the file that are relevant
        to the goals (they contribute at least one point to the metric tracked
        by some goal) but aren't listed due to the limit on items displayed.
        This function has some heuristics to ensure that the items that
        contribute the most to each goal metric get printed in full rather than
        being rolled up into `other_count`.
        """
        all_keys = set(g.key for g in self.goals)

        def key_str(x):
            if isinstance(x, str):
                return x
            elif isinstance(x, (list, tuple)):
                return '.'.join(x)
            else:
                raise TypeError(f'expected str or tuple[str, str], but got {type(x)}')

        contributor_metrics = defaultdict(lambda: defaultdict(int))
        file_contributors = defaultdict(set)
        goal_contributors = []
        for i, goal in enumerate(self.goals):
            # Skip goals that are already achieved.  If the target is 10 and
            # the current value is 5, we don't include the items contributing
            # to the 5, or show that metric if an item is included otherwise.
            if goal.count_excess(mvir, unsafe_json) == 0:
                continue

            current_goal_contributors = []
            for kind, name, val, cost in goal._iter_matching_items_with_cost(mvir, unsafe_json):
                if cost <= 0:
                    continue
                filename = val['filename']
                contributor_metrics[name][key_str(goal.key)] = cost
                file_contributors[filename].add(name)
                current_goal_contributors.append((cost, name))
            # Sort by increasing cost.
            current_goal_contributors.sort(key = lambda x: x[0])
            goal_contributors.append(current_goal_contributors)

        selected = set()
        LIMIT = 50
        for gc in itertools.cycle(goal_contributors):
            if len(gc) == 0:
                continue
            _cost, name = gc.pop()
            selected.add(name)
            if len(selected) >= LIMIT or len(selected) >= len(contributor_metrics):
                # We either hit the limit or exhausted all contributors.
                # (`contributor_metrics` has one entry for every contributor.)
                break

        out = []
        for filename, item_names in sorted(file_contributors.items()):
            items = []
            other_count = 0
            for name in sorted(item_names):
                if name in selected:
                    metrics_str = ', '.join(
                        f'{k} = {v}' for k,v in contributor_metrics[name].items())
                    items.append({
                        'name': name,
                        'metrics': metrics_str,
                    })
                else:
                    other_count += 1
            out.append({
                'filename': filename,
                'items': items,
                'other_count': other_count,
            })

        return out

    def to_cbor(self):
        dct = {
            'description': self.description,
            'goals': [g.to_cbor() for g in self.goals],
        }
        if len(self.details) > 0:
            dct['details'] = self.details
        if self.subdiv_attempts != 0:
            dct['subdiv_attempts'] = self.subdiv_attempts
        return dct

    @classmethod
    def from_cbor(cls, raw):
        return cls(
            description = raw['description'],
            goals = [StepGoal.from_cbor(g) for g in raw.get('goals', ())],
            details = raw.get('details') or [],
            subdiv_attempts = raw.get('subdiv_attempts', 0)
                + raw.get('indivisible', False),    # TODO: migration - remove
        )

    def persistent_fields(self):
        """
        Get the fields that should never be mutated, which can be used to
        identify this plan.
        """
        return (
            self.description,
            self.goals,
        )
