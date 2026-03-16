use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;
use clap::Parser;
use rust_util::collect::FileCollector;
use rust_util::mod_path::ModPath;
use rust_util::rewrite;
use syn::{File, Item, Visibility};

/// Split a Rust codebase into a JSON map from item paths to their source text.
#[derive(Parser)]
struct Args {
    /// Root Rust source file to split (`lib.rs` or `main.rs`).
    src_root_path: PathBuf,
}

/// Process a module and return a flag indicating whether it has any interesting contents.  This
/// will recursively visit each child module and delete any that are uninteresting (which may in
/// turn cause the parent to become uninteresting).
///
/// This keeps a record of all modules it deleted so that the corresponding files can be removed
/// afterward.
fn process_module(
    mod_path: ModPath,
    items: &mut Vec<Item>,
    module_asts: &mut HashMap<ModPath, File>,
    deleted_modules: &mut Vec<ModPath>,
) -> bool {
    let mut interesting = false;
    items.retain_mut(|item| {
        match *item {
            Item::Mod(ref mut item) => {
                let mut child_path = mod_path.clone();
                child_path.push(&item.ident.to_string());

                let child_interesting = if let Some((_, ref mut child_items)) = item.content {
                    process_module(child_path.clone(), child_items,
                        module_asts, deleted_modules)
                } else {
                    process_module_ast(child_path.clone(), module_asts, deleted_modules)
                };

                // Keep the child module only if it's interesting.
                if child_interesting {
                    interesting = true;
                } else {
                    deleted_modules.push(child_path);
                }
                child_interesting
            },

            Item::Use(ref mut item) => {
                match item.vis {
                    Visibility::Inherited => {
                        // Non-`pub` imports are uninteresting.
                    },
                    Visibility::Public(..) | Visibility::Restricted(..) => {
                        interesting = true;
                    },
                }
                // Always keep the `use` item, whether or not it's interesting.
                true
            },

            _ => {
                // All other item kinds are considered interesting and are kept.
                interesting = true;
                true
            },
        }
    });
    interesting
}

fn process_module_ast(
    mod_path: ModPath,
    module_asts: &mut HashMap<ModPath, File>,
    deleted_modules: &mut Vec<ModPath>,
) -> bool {
    let mut ast = module_asts.remove(&mod_path)
        .unwrap_or_else(|| panic!("missing entry for {mod_path:?}"));
    let interesting =
        process_module(mod_path.clone(), &mut ast.items, module_asts, deleted_modules);
    debug_assert!(!module_asts.contains_key(&mod_path),
        "inserted duplicate entry for {mod_path:?} during traversal");
    module_asts.insert(mod_path.clone(), ast);
    interesting
}

fn main() {
    let args = Args::parse();
    let mut fc = FileCollector::default();
    fc.parse(args.src_root_path, vec![], true).unwrap();

    let mut module_asts = HashMap::new();
    let mut module_file_paths = HashMap::new();
    for (file_path, mod_path, ast) in fc.files {
        let mod_path = ModPath::from_iter(mod_path.iter().map(|s| s as &str));
        eprintln!("visit {mod_path:?}");
        debug_assert!(!module_file_paths.contains_key(&mod_path),
            "duplicate entry for {mod_path:?}");
        module_file_paths.insert(mod_path.clone(), file_path);
        debug_assert!(!module_asts.contains_key(&mod_path),
            "duplicate entry for {mod_path:?}");
        module_asts.insert(mod_path, ast);
    }

    let orig_module_asts = module_asts.clone();

    let root_mod_path = ModPath::new("".into());
    let mut deleted_modules = Vec::new();
    let _ = process_module_ast(root_mod_path.clone(), &mut module_asts, &mut deleted_modules);

    for mod_path in deleted_modules {
        eprintln!("deleted module {mod_path:?}");
        module_asts.remove(&mod_path);
        if let Some(file_path) = module_file_paths.get(&mod_path) {
            eprintln!("  delete {file_path:?}");
            fs::remove_file(file_path).unwrap();
        }
    }

    for (mod_path, new_ast) in module_asts {
        let file_path = module_file_paths.get(&mod_path).unwrap();
        eprintln!("rewriting {mod_path:?} @ {file_path:?}");
        let orig_src = fs::read_to_string(file_path).unwrap();
        let orig_ast = orig_module_asts.get(&mod_path).unwrap();
        let new_src = rewrite::rewrite_file(&orig_src, orig_ast, &new_ast);
        fs::write(file_path, new_src).unwrap();
    }
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn example1() {
        let mut module_asts = HashMap::<_, File>::new();
        const ROOT_MOD_SRC: &'static str = r"
            mod interesting {
                use std::clone::Clone;
                fn f() {
                }
                mod uninteresting_nested {
                }
            }

            mod uninteresting {
                use std::clone::Clone;
                mod uninteresting_nested {
                }
            }
        ";
        module_asts.insert(ModPath::new("".into()), syn::parse_str(ROOT_MOD_SRC).unwrap());

        let root_mod_path = ModPath::new("".into());
        let orig_root_ast = module_asts.get(&root_mod_path).unwrap().clone();

        let mut deleted_modules = Vec::new();
        let _ = process_module_ast(root_mod_path.clone(), &mut module_asts, &mut deleted_modules);
        insta::assert_debug_snapshot!(deleted_modules);

        let new_root_ast = module_asts.remove(&root_mod_path).unwrap();
        let new_src = rewrite::rewrite_file(ROOT_MOD_SRC, &orig_root_ast, &new_root_ast);
        insta::assert_snapshot!(new_src);
    }
}
