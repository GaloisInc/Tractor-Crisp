#![feature(rustc_private)]
extern crate rustc_public;

// Required by rustc_public::run! macro
extern crate rustc_driver;
extern crate rustc_interface;
extern crate rustc_middle;

use std::collections::HashSet;
use std::env;
use std::fs::{self, File};
use std::path::Path;
use indexmap::{IndexMap, IndexSet};
use regex::RegexSet;
use rustc_public::{CrateDef, ItemKind};
use rustc_public::error::CompilerError;
use serde::{Serialize, Deserialize};
use serde_json;


#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(untagged)]
pub enum NameFilter {
    Literals(Vec<String>),
    Regex(String),
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Filter {
    #[serde(default)]
    pub files: Option<NameFilter>,
    #[serde(default)]
    pub functions: Option<NameFilter>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Module {
    pub filters: Vec<Filter>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MigrateModules {
    pub modules: Vec<Module>,
}


#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ExpandedModule {
    pub items: Vec<String>,
    pub total_size: usize,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ExpandedMigrateModules {
    pub modules: Vec<ExpandedModule>,
    pub other_items: IndexMap<String, Vec<String>>,
    /// Files that contained items but weren't matched by any `files` filter.
    pub other_files: Vec<String>,
}


struct RegexMap<T> {
    regex_set: RegexSet,
    /// If the `RegexSet` reports a match on regex `i`, the resulting value is `values[i]`.
    values: Vec<T>,
}

impl<T> RegexMap<T> {
    pub fn new<S: AsRef<str>>(entries: impl IntoIterator<Item = (S, T)>) -> RegexMap<T> {
        let entries = entries.into_iter();
        let mut values = Vec::new();
        let regex_set = RegexSet::new(entries.map(|(re_str, val)| {
            values.push(val);
            re_str
        })).unwrap();
        RegexMap { regex_set, values }
    }

    pub fn matches<'a>(&'a self, haystack: &str) -> RegexMapMatches<'a, T> {
        RegexMapMatches {
            matches: self.regex_set.matches(haystack),
            values: &self.values,
        }
    }
}

struct RegexMapMatches<'a, T> {
    matches: regex::SetMatches,
    values: &'a [T],
}

impl<'a, T> RegexMapMatches<'a, T> {
    pub fn iter<'b>(&'b self) -> impl Iterator<Item = &'a T> + 'b {
        let values = self.values;
        self.matches.iter().map(move |idx| &values[idx])
    }
}


struct Index {
    /// Index that maps file names to `(module index, filter index)` pairs.  If this maps string
    /// `s` to the pair `(i, j)`, that means `s` matches `mm.modules[i].filters[j].files`.
    files: RegexMap<(usize, usize)>,
    /// Compiled `RegexSet`s for all function filters.  If `s` matches `functions[i][j]`, then it
    /// also matches the `NameFilter` `mm.modules[i].filters[j].functions`.
    functions: Vec<Vec<RegexSet>>,
}

impl Index {
    /// Call `f` on the index of each module from `mm` that has at least one `Filter` matching
    /// `filename` and `function_name`.
    fn for_each_module(
        &self,
        filename: &str,
        function_name: &str,
        mut f: impl FnMut(usize),
    ) -> bool {
        let mut seen = HashSet::new();
        let mut file_matched = false;
        for &(i, j) in self.files.matches(filename).iter() {
            file_matched = true;
            if self.functions[i][j].is_match(function_name) {
                if seen.insert(i) {
                    f(i);
                }
            }
        }
        file_matched
    }
}

fn build_index(mm: &MigrateModules) -> Index {
    let mut file_entries = Vec::new();
    let mut functions = Vec::new();
    for (i, m) in mm.modules.iter().enumerate() {
        let mut module_functions = Vec::new();
        for (j, f) in m.filters.iter().enumerate() {
            if let Some(ref nf) = f.files {
                for_each_name_filter_regex(nf, |re_str| {
                    file_entries.push((re_str, (i, j)));
                });
            } else {
                file_entries.push((".*".into(), (i, j)));
            }

            let mut function_entries = Vec::new();
            if let Some(ref nf) = f.functions {
                for_each_name_filter_regex(nf, |re_str| {
                    function_entries.push(format!(r"^(.*::)?{re_str}"));
                });
            } else {
                function_entries.push(".*".into());
            }
            let function_regex_set = RegexSet::new(function_entries).unwrap();
            module_functions.push(function_regex_set);
        }
        functions.push(module_functions);
    }
    Index {
        files: RegexMap::new(file_entries),
        functions,
    }
}

fn for_each_name_filter_regex(nf: &NameFilter, mut f: impl FnMut(String)) {
    match *nf {
        NameFilter::Literals(ref lits) => {
            for lit in lits {
                f(regex::escape(lit));
            }
        },
        NameFilter::Regex(ref re) => f(re.clone()),
    }
}


fn main() {
    let src_dir = env::var("FIND_UNSAFE2_SRC_DIR").unwrap();
    let src_dir = Path::new(&src_dir);
    assert!(src_dir.is_absolute(),
        "expected $FIND_UNSAFE2_SRC_DIR to be an absolute path, but got {:?}", src_dir);

    let modules_path = env::var("EXPAND_MODULES_JSON_PATH").unwrap();
    let modules_path = Path::new(&modules_path);
    assert!(modules_path.is_absolute(),
        "expected $EXPAND_MODULES_JSON_PATH to be an absolute path, but got {:?}",
        modules_path);

    let output_dir = env::var("EXPAND_MODULES_JSON_OUTPUT_DIR").unwrap();
    let output_dir = Path::new(&output_dir);
    assert!(output_dir.is_absolute(),
        "expected $EXPAND_MODULES_JSON_OUTPUT_DIR to be an absolute path, but got {:?}",
        output_dir);
    fs::create_dir_all(&output_dir).unwrap();


    let modules_file = File::open(modules_path).unwrap();
    let mm: MigrateModules = serde_json::from_reader(modules_file).unwrap();

    let index = build_index(&mm);


    let args = env::args().collect::<Vec<_>>();
    let r = rustc_public::run_with_tcx!(&args[1..], |_tcx| {
        let crate_name = rustc_public::local_crate().name;

        // Skip build.rs files.  Cargo always uses this name when compiling build.rs.
        if crate_name == "build_script_build" {
            return ControlFlow::<(), ()>::Continue(());
        }

        let mut found_src = false;
        let mut files_seen = HashSet::new();
        let items = rustc_public::all_local_items();
        for &item in &items {
            let file = item.span().get_filename();
            if files_seen.insert(file.clone()) {
                if let Ok(file_abs) = Path::new(&file).canonicalize() {
                    if file_abs.starts_with(&src_dir) {
                        found_src = true;
                        break;
                    }
                }
            }
        }

        // Only process the current crate if it's inside the `SRC_DIR`.
        if !found_src {
            return ControlFlow::<(), ()>::Continue(());
        }

        let mut modules = vec![ExpandedModule {
            items: Vec::new(),
            total_size: 0,
        }; mm.modules.len()];
        let mut other_items = IndexMap::new();

        let mut files_seen = IndexSet::new();
        let mut files_matched = HashSet::new();

        for item in items {
            if !item.has_body() {
                continue;
            }
            if item.kind() != ItemKind::Fn {
                continue;
            }

            let name = item.name();
            let span = item.span();
            let filename = span.get_filename();

            // For functions, the main span covers only the signature.
            let body_span = item.expect_body().span;
            let lines = body_span.get_lines();
            let size = lines.end_line - lines.start_line + 1;

            if !files_seen.contains(&filename) {
                files_seen.insert(filename.clone());
            }

            let mut found_any_module = false;
            let file_matched = index.for_each_module(&filename, &name, |mod_idx| {
                let m = &mut modules[mod_idx];
                m.items.push(name.clone());
                m.total_size += size;
                found_any_module = true;
            });
            if file_matched && !files_matched.contains(&filename) {
                files_matched.insert(filename.clone());
            }

            if !found_any_module {
                other_items.entry(filename).or_insert(Vec::new()).push(name);
            }
        }

        let emm = ExpandedMigrateModules {
            modules,
            other_items,
            other_files: files_seen.into_iter().filter(|f| !files_matched.contains(&*f)).collect(),
        };

        let out_path = output_dir.join(format!("{crate_name}.json"));
        serde_json::to_writer(
            File::create(&out_path).unwrap(),
            &emm,
        ).unwrap();

        ControlFlow::<(), ()>::Continue(())
    });

    match r {
        Ok(()) => {},
        Err(CompilerError::Failed) => panic!("compilation failed"),
        Err(CompilerError::Interrupted(())) => {},
        Err(CompilerError::Skipped) => {},
    }


}
