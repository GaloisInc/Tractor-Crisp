use clap::Parser;
use indexmap::IndexMap;
use rust_util::collect::FileCollector;
use rust_util::item_span::item_spans;
use serde_json;
use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::iter;
use std::path::{Path, PathBuf};
use syn;

/// Merge updated item definitions into a Rust codebase.
#[derive(Parser)]
struct Args {
    /// Root Rust source file to update (`lib.rs` or `main.rs`).
    src_root_path: PathBuf,
    /// JSON file containing mapping from Rust item paths to desired new contents.
    new_snippets_file: PathBuf,

    /// Use the JSON contents to overwrite existing definitions, but don't add or remove anything.
    #[clap(long)]
    update_only: bool,
}

type ModPath = String;

fn main() {
    let args = Args::parse();
    let src_root_path = args.src_root_path;
    let src_root_dir = Path::new(&src_root_path).parent().unwrap();
    let new_snippet_json_path = args.new_snippets_file;

    let new_snippets_file = File::open(&new_snippet_json_path).unwrap();
    let new_snippets: IndexMap<String, String> = serde_json::from_reader(new_snippets_file).unwrap();

    let mut fc = FileCollector::default();
    fc.parse(&src_root_path, vec![], true).unwrap();

    let mut file_map: HashMap<ModPath, (PathBuf, syn::File)> = fc.files.into_iter()
        .map(|(file_path, mod_path_parts, ast)| (mod_path_parts.join("::"), (file_path, ast)))
        .collect();
    let mut snippet_map: HashMap<ModPath, IndexMap<String, String>> = HashMap::new();
    for (item_path, text) in new_snippets {
        let (mod_path, item_name) = match item_path.rfind("::") {
            Some(idx) => {
                let item_name = item_path[idx + 2 ..].to_owned();
                let mut mod_path = item_path;
                mod_path.truncate(idx);
                (mod_path, item_name)
            },
            None => (String::new(), item_path),
        };
        let old = snippet_map.entry(mod_path).or_insert_with(IndexMap::new)
            .insert(item_name, text);
        assert!(old.is_none(), "duplicate entry (old = {:?})", old);
    }

    // For every module mentioned in `snippet_map`, if the module doesn't exist in `file_map`,
    // create it.
    let mut snippet_map_keys = snippet_map.keys().cloned().collect::<Vec<_>>();
    snippet_map_keys.sort();
    for mod_path in snippet_map_keys {
        // Iterate over all ancestors of `mod_path`.
        let idxs = iter::once(mod_path.len())
            .chain(mod_path.rmatch_indices("::").map(|(idx, _)| idx));
        for idx in idxs {
            let mod_path = &mod_path[..idx];
            if file_map.contains_key(mod_path) {
                continue;
            }

            // Create an empty file on disk and add it to `file_map`.
            debug_assert!(mod_path.len() != 0);
            let file_path_rel = mod_path.replace("::", "/") + ".rs";
            let file_path = src_root_dir.join(&file_path_rel);
            assert!(!fs::exists(&file_path).unwrap(),
                "file {:?} is missing from file_map, but exists on disk?", file_path);
            fs::write(&file_path, "").unwrap();
            let ast = syn::File {
                shebang: None,
                attrs: Vec::new(),
                items: Vec::new(),
            };
            file_map.insert(mod_path.to_owned(), (file_path, ast));

            // Add a `mod foo;` snippet to the parent module.
            let (parent_mod_path, mod_name) = mod_path.rsplit_once("::")
                .unwrap_or(("", mod_path));
            let old = snippet_map.entry(parent_mod_path.to_owned()).or_insert_with(IndexMap::new)
                .insert(mod_name.to_owned(), format!("mod {mod_name};"));
            assert!(old.is_none(), "item {:?} exists but is not a module", mod_path);
        }
    }

    // Update files using snippets.
    let no_snippets = IndexMap::new();
    for (mod_path, &(ref file_path, ref ast)) in &file_map {
        let snippets = snippet_map.get(mod_path).unwrap_or(&no_snippets);

        eprintln!("visit {file_path:?}");
        let old_src = fs::read_to_string(file_path).unwrap();

        let mut rewrites = Vec::new();

        // Update or remove existing items.
        let mod_path_parts = if mod_path == "" {
            Vec::new()
        } else {
            mod_path.split("::").map(|s| s.to_owned()).collect::<Vec<String>>()
        };
        let mut snippets_applied = HashSet::new();
        for (item_path, lo, hi) in item_spans(mod_path_parts, ast) {
            let old_snippet = &old_src[lo..hi];
            let item_path_str = item_path.join("::");
            let new_snippet = match snippets.get(&item_path_str) {
                Some(x) => {
                    snippets_applied.insert(item_path_str);
                    x
                },
                None => {
                    if args.update_only {
                        // We would normally delete this item, but we're currently in update-only
                        // mode.
                        continue;
                    } else {
                        ""
                    }
                },
            };
            if new_snippet != old_snippet {
                rewrites.push((lo, hi, new_snippet));
            }
        }

        // Add new items (unless we're in update-only mode).
        if !args.update_only {
            let end = old_src.len();
            for (item_path_str, snippet) in snippets {
                if snippets_applied.contains(item_path_str) {
                    continue;
                }
                rewrites.push((end, end, "\n\n"));
                rewrites.push((end, end, snippet));
            }
        }

        if rewrites.len() == 0 {
            continue;
        }

        // Apply rewrites
        rewrites.sort_by_key(|&(lo, _, _)| lo);
        let mut new_src = String::with_capacity(old_src.len());
        let mut pos = 0;
        for &(lo, hi, new_snippet) in &rewrites {
            assert!(
                lo >= pos,
                "overlapping rewrites: previous rewrite ended at {}, \
                but current rewrite covers {} .. {}",
                pos,
                lo,
                hi
            );
            new_src.push_str(&old_src[pos..lo]);
            new_src.push_str(new_snippet);
            pos = hi;
        }
        new_src.push_str(&old_src[pos..]);

        let tmp_path = file_path.with_extension(".new");
        fs::write(&tmp_path, &new_src).unwrap();
        fs::rename(&tmp_path, file_path).unwrap();
        eprintln!("applied {} rewrites to {:?}", rewrites.len(), file_path);
    }
}
