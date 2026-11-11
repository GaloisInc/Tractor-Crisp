from collections import defaultdict
from dataclasses import dataclass
import random

from .workflow import FuelCounter, AgentTargetField, AgentTargetFunction, AgentTargetOther

class PickTarget:
    def __init__(self, limits):
        self.fuel_file = FuelCounter('target file',
            default_give=limits.safety_tries_per_file)
        self.fuel_target = FuelCounter('target goal',
            default_give=limits.safety_tries_per_target)
        self.file_target_goals = []
        self.current_file = None
        self.target_goal = None

    def current_target_goal(self, w, n_code):
        # Keep going with the current goal if possible.

        # Optimistically take fuel from both the target and file to continue
        # with the current goal.  If the target is out of fuel, we change
        # targets and `give()` the target more fuel below; if the file is out
        # of fuel, we changes files and targets and `give()` both more fuel.
        target_had_fuel = self.fuel_target.try_use()
        file_had_fuel = self.fuel_file.try_use()

        if (
            file_had_fuel and target_had_fuel
            and self.target_goal is not None
            and not target_goal_is_done(w, n_code, self.target_goal)
        ):
            print(f'PickTarget: continue with {self.target_goal} from {self.current_file}')
            print(f'  target fuel: {self.fuel_target.fuel}')
            print(f'  file fuel: {self.fuel_file.fuel}')
            return self.target_goal

        # Try to pick a goal from the current file if there's fuel left for it.
        if file_had_fuel:
            while len(self.file_target_goals) > 0:
                candidate_goal = self.file_target_goals.pop()
                if not target_goal_is_done(w, n_code, candidate_goal):
                    self.target_goal = candidate_goal
                    self.fuel_target.give()
                    self.fuel_target.use()
                    print(f'PickTarget: new target {self.target_goal} from {self.current_file}')
                    print(f'  target fuel: {self.fuel_target.fuel}')
                    print(f'  file fuel: {self.fuel_file.fuel}')
                    return self.target_goal

        # Switch files, and build a new list of targets.
        self.current_file, self.file_target_goals = pick_file_and_list_targets(w, n_code)
        if len(self.file_target_goals) > 0:
            # No need to check `target_goal_is_done`, since none of the goals
            # in the list have been fixed yet.
            self.target_goal = self.file_target_goals.pop()
            self.fuel_file.give()
            self.fuel_file.use()
            self.fuel_target.give()
            self.fuel_target.use()
            print(f'PickTarget: new file {self.current_file}, target {self.target_goal}')
            print(f'  target fuel: {self.fuel_target.fuel}')
            print(f'  file fuel: {self.fuel_file.fuel}')
            return self.target_goal

        # Found no goal in any file, but `count_unsafe2` still reported some
        # unsafe code.  Tell the agent to check the entire codebase for
        # leftover unsafety.
        self.target_goal = AgentTargetOther()
        self.fuel_file.give()
        self.fuel_file.use()
        self.fuel_target.give()
        self.fuel_target.use()
        print('PickTarget: no valid targets')
        return self.target_goal


def pick_file_and_list_targets(w, n_code):
    # Choose a `target_goal`.
    @dataclass(frozen = True)
    class FileTargets:
        functions: list
        fields: list
    files = defaultdict(lambda: FileTargets([], []))
    for n_json_file in w.find_unsafe2_json_files(n_code):
        j = n_json_file.body_json()
        for fn_name, j_fn in j['fns'].items():
            if j_fn['total_unsafe'] == 0 or j_fn.get('ffi_symbol') is not None:
                continue
            files[j_fn['filename']].functions.append(AgentTargetFunction(fn_name))
        for type_name, j_type in j['types'].items():
            if 'type' in j_type['field_contains_raw_ptr']:
                # This is a type alias, not a struct
                # definition.  To avoid confusing prompting, we
                # omit this from `target_fields`.  (Otherwise
                # we would end up telling the agent to fix the
                # "type" field of a non-struct.)
                continue
            files[j_type['filename']].fields.extend(AgentTargetField(type_name, k)
                for k,v in j_type['field_contains_raw_ptr'].items()
                if v > 0)

    # Exclude any files that are already fully safe.
    files = {k: v for k,v in files.items() if len(v.functions) + len(v.fields) > 0}

    if len(files) == 0:
        # We found no unsafe fields or functions.
        return None, []

    print(f'pick_file_and_list_targets: picking from {len(files)} files')
    filename, file_targets = random.choice(list(files.items()))
    functions = file_targets.functions
    fields = file_targets.fields
    print(f'  picked file {filename} with {len(functions)} function targets '
        f'and {len(fields)} field targets')

    # We have a limited per-file budget, and we prefer to spend it on fields
    # rather than functions, so we try to put fields earlier in the final list.
    order = []

    # Shuffle functions and fields so each one has equal priority within its
    # category.
    random.shuffle(functions)
    random.shuffle(fields)

    while len(functions) > 0 and len(fields) > 0:
        # Prefer fields over functions by giving them 5x normal weight.
        functions_weight = len(functions)
        fields_weight = 5 * len(fields)
        total_weight = functions_weight + fields_weight
        x = random.randrange(total_weight)
        if x < functions_weight:
            order.append(functions.pop())
        else:
            order.append(fields.pop())

    order.extend(fields)
    order.extend(functions)

    # `PickTarget` actually processes the list in reverse order, so reverse it
    # to put higher-priority items near the end.
    order.reverse()

    print(f'  order ({len(order)} items):')
    for x in order:
        print(f'    {x}')
    return filename, order

def target_goal_is_done(w, n_code, target_goal):
    total = 0
    for n_json_file in w.find_unsafe2_json_files(n_code):
        j = n_json_file.body_json()
        match target_goal:
            case AgentTargetField(struct, field):
                j_struct = j['types'].get(struct)
                if j_struct is not None:
                    total += j_struct['field_contains_raw_ptr'].get(field, 0)
            case AgentTargetFunction(func):
                j_func = j['fns'].get(func)
                if j_func is not None:
                    total += j_func['total_unsafe']
            case AgentTargetOther():
                total += j['total_unsafe']
    return total == 0
