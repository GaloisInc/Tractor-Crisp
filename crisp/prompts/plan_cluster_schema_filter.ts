// A name filter is either a regex or a list of regexes.  A name matches the
// filter if it matches any of the regexes.
type NameFilter = string | string[];

type Filter = {
  // If set, this filter matches only definitions from files with matching names.
  // All paths should be relative to the root of the cargo workspace, e.g. `src/lib.rs`.
  files?: NameFilter,
  // If set, this filter matches only functions with matching names.
  functions?: NameFilter,
};
