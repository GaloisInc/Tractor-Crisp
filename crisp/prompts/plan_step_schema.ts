// A string is a Python regex matched from the start of the name. An array
// contains literal names (also matched from the start). Omit to match all names.
type NameFilter = string | string[];

type StepGoal = {
  // An inventory counter, e.g. "derefs_raw_ptr", "is_unsafe_fn", or
  // "field_contains_raw_ptr". A map counter sums all its entries; a pair
  // selects one entry, e.g. ["uses_foreign_fn", "libc::printf"].
  key: string | [string, string],
  // The goal is met when the matching items' total is <= this limit (default 0).
  max_count?: number,
  // Paths are relative to the cargo workspace root, e.g. "src/lib.rs".
  files?: NameFilter,
  // Applies to function records; FFI entry points are excluded.
  functions?: NameFilter,
  // Applies to type records. Function and type filters act independently.
  types?: NameFilter,
};

type StepPlan = {
  // Describe the transformation, dependencies, and semantic done-criteria.
  desc: string,
  // All goals must be met to complete this step. Missing counters count as zero.
  goals: StepGoal[],
};
