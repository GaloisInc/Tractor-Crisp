use crate::error::Error;
use proc_macro2::Span;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::iter;
use std::mem;
use std::path::{Path, PathBuf};
use syn;
use syn::ext::IdentExt;
use syn::spanned::Spanned;

#[derive(Clone, Debug)]
pub struct CrateInfo {
    pub root_file_path: PathBuf,
    pub modules: Vec<ModInfo>,
}

#[derive(Clone, Debug)]
pub struct ModInfo {
    pub mod_path: Vec<String>,
    pub file_path: PathBuf,
    pub inner_end_pos: usize,
    pub is_inline: bool,
}

#[derive(Clone, Default)]
pub struct FileCollector {
    /// File path, module path, and AST for each file visited so far.
    pub files: Vec<(PathBuf, Vec<String>, syn::File)>,
    pub mods: Vec<ModInfo>,
    /// Inline modules collected by `walk_items`.  These are converted into `ModInfo`s in the
    /// enclosing call to `parse`.
    inline_mods: Vec<(Vec<String>, Span)>,
    seen: HashSet<PathBuf>,
}

impl FileCollector {
    pub fn parse(
        &mut self,
        file_path: impl AsRef<Path>,
        mod_path: Vec<String>,
        is_root: bool,
    ) -> Result<(), Error> {
        let file_path = file_path.as_ref();
        if self.seen.contains(file_path) {
            return Ok(());
        }
        let src = fs::read_to_string(file_path)
            .map_err(|e| Error::from(e).at(format_args!("reading {file_path:?}")))?;
        let ast: syn::File = syn::parse_file(&src)
            .map_err(|e| Error::from(e).at(format_args!("parsing {file_path:?}")))?;
        // Set `seen` immediately, but don't add to `files` (and give up ownership) until we're
        // done walking `ast`.
        self.seen.insert(file_path.to_owned());
        let is_mod_rs = is_root || file_path.file_name().is_some_and(|n| n == "mod.rs");
        let base_path_storage;
        let base_path = if is_mod_rs {
            file_path
                .parent()
                .ok_or_else(|| format!("mod.rs path {file_path:?} has no parent"))?
        } else {
            base_path_storage = file_path.with_extension("");
            &base_path_storage
        };

        let old_inline_mods = mem::take(&mut self.inline_mods);
        self.walk_items(&ast.items, base_path, mod_path.clone(), &[])?;
        let new_inline_mods = mem::replace(&mut self.inline_mods, old_inline_mods);

        self.mods.push(ModInfo {
            mod_path: mod_path.clone(),
            file_path: file_path.to_owned(),
            inner_end_pos: ast.span().byte_range().end as usize,
            is_inline: false,
        });
        for (mod_path, span) in new_inline_mods {
            self.mods.push(ModInfo {
                mod_path: mod_path,
                file_path: file_path.to_owned(),
                inner_end_pos: span.byte_range().end as usize - 1,
                is_inline: true,
            });
        }
        self.files.push((file_path.to_owned(), mod_path, ast));

        Ok(())
    }

    fn walk_items(
        &mut self,
        items: &[syn::Item],
        base_path: &Path,
        mut mod_path: Vec<String>,
        parent_module: &[&str],
    ) -> Result<(), Error> {
        for item in items {
            let im = match *item {
                syn::Item::Mod(ref im) => im,
                _ => continue,
            };
            mod_path.push(im.ident.unraw().to_string());
            if let Some((brace, ref inline_items)) = im.content {
                let name =
                    path_attr_value(&im.attrs)?.unwrap_or_else(|| im.ident.unraw().to_string());
                let module = parent_module
                    .iter()
                    .copied()
                    .chain(iter::once(&name as &_))
                    .collect::<Vec<_>>();
                self.walk_items(inline_items, base_path, mod_path.clone(), &module)?;
                self.inline_mods.push((mod_path.clone(), brace.span.join()));
            } else {
                let mut path = base_path.to_owned();
                for &m in parent_module {
                    path.push(m);
                }
                if let Some(attr_path) = path_attr_value(&im.attrs)? {
                    path.push(attr_path);
                    self.parse(path, mod_path.clone(), false)?;
                } else {
                    let name = im.ident.unraw().to_string();
                    // Try `foo/mod.rs` first; if it doesn't exist, try `foo.rs` instead.
                    path.push(name);
                    path.push("mod.rs");
                    if !fs::exists(&path)? {
                        path.pop();
                        path.set_extension("rs");
                    }
                    self.parse(path, mod_path.clone(), false)?;
                }
                // No need to update `self.mods` - that's handled by the recursive call to `parse`.
            }
            mod_path.pop();
        }
        Ok(())
    }
}


trait Filesystem {
    fn read_to_string(&self, path: &Path) -> Result<String, Error>;
    fn exists(&self, path: &Path) -> Result<bool, Error>;
}

struct NativeFilesystem;

impl Filesystem for NativeFilesystem {
    fn read_to_string(&self, path: &Path) -> Result<String, Error> {
        Ok(fs::read_to_string(path)?)
    }
    fn exists(&self, path: &Path) -> Result<bool, Error> {
        Ok(fs::exists(path)?)
    }
}


struct ChildMod {
    rel_mod_path: Vec<String>,
    file_path: PathBuf,
    inner_end_pos: usize,
    is_inline: bool,
}

fn walk_file(
    fs: &mut impl Filesystem,
    file_path: impl AsRef<Path>,
    is_root: bool,
) -> Result<(syn::File, Vec<ChildMod>), Error> {
    let file_path = file_path.as_ref();
    let src = fs.read_to_string(file_path)
        .map_err(|e| e.at(format_args!("reading {file_path:?}")))?;
    let ast: syn::File = syn::parse_file(&src)
        .map_err(|e| Error::from(e).at(format_args!("parsing {file_path:?}")))?;

    let is_mod_rs = is_root || file_path.file_name().is_some_and(|n| n == "mod.rs");
    let base_path_storage;
    let base_path = if is_mod_rs {
        file_path
            .parent()
            .ok_or_else(|| format!("mod.rs path {file_path:?} has no parent"))?
    } else {
        base_path_storage = file_path.with_extension("");
        &base_path_storage
    };

    let mut child_mods = Vec::new();
    walk_items(fs, &ast.items, file_path, base_path, &[], &mut child_mods)?;

    Ok((ast, child_mods))
}

fn walk_items(
    fs: &mut impl Filesystem,
    items: &[syn::Item],
    file_path: &Path,
    search_path: &Path,
    parent_rel_mod_path: &[String],
    child_mods: &mut Vec<ChildMod>,
) -> Result<(), Error> {
    for item in items {
        let im = match *item {
            syn::Item::Mod(ref im) => im,
            _ => continue,
        };

        let name = im.ident.unraw().to_string();
        let rel_mod_path = parent_rel_mod_path.iter().cloned()
            .chain(iter::once(name.clone()))
            .collect::<Vec<_>>();

        if let Some((brace, ref inline_items)) = im.content {
            let path_name = path_attr_value(&im.attrs)?.unwrap_or_else(|| name.clone());
            let new_search_path = search_path.join(path_name);

            walk_items(fs, inline_items, file_path, &new_search_path, &rel_mod_path, child_mods)?;
            child_mods.push(ChildMod {
                rel_mod_path,
                file_path: file_path.to_owned(),
                inner_end_pos: brace.span.close().byte_range().end as usize,
                is_inline: true,
            });

        } else {
            let mut path = search_path.to_owned();
            if let Some(attr_path) = path_attr_value(&im.attrs)? {
                path.push(attr_path);
            } else {
                // Try `foo/mod.rs` first; if it doesn't exist, try `foo.rs` instead.
                path.push(&name);
                path.push("mod.rs");
                if !fs.exists(&path)? {
                    path.pop();
                    path.set_extension("rs");
                }
            }

            child_mods.push(ChildMod {
                rel_mod_path,
                file_path: path,
                inner_end_pos: 0,
                is_inline: false,
            });
        }
    }
    Ok(())
}

fn path_attr_value(attrs: &[syn::Attribute]) -> Result<Option<String>, Error> {
    for attr in attrs {
        if !attr.meta.path().is_ident("path") {
            continue;
        }
        let mnv = match attr.meta {
            syn::Meta::NameValue(ref x) => x,
            _ => return Err("expected `path` attribute to have a value".into()),
        };
        let el = match mnv.value {
            syn::Expr::Lit(ref x) => x,
            _ => return Err("expected `path` attribute value to be a literal".into()),
        };
        let ls = match el.lit {
            syn::Lit::Str(ref x) => x,
            _ => return Err("expected `path` attribute value to be a string literal".into()),
        };
        return Ok(Some(ls.value()));
    }
    Ok(None)
}


/// Try calling `walk_file` on `file_path` with both settings for `is_root`, and return the results
/// from whichever one refers to fewer nonexistent files.
fn walk_file_guess_root(
    fs: &mut impl Filesystem,
    file_path: impl AsRef<Path>,
) -> Result<(syn::File, Vec<ChildMod>, bool), Error> {
    fn count_nonexistent_children(
        fs: &mut impl Filesystem,
        child_mods: &[ChildMod],
    ) -> Result<usize, Error> {
        let mut num_nonexistent_children = 0;
        for cm in child_mods {
            if cm.is_inline {
                continue;
            }
            if !fs.exists(&cm.file_path)? {
                num_nonexistent_children += 1;
            }
        }
        Ok(num_nonexistent_children)
    }

    let file_path = file_path.as_ref();

    let (ast, child_mods) = walk_file(fs, file_path, false)?;
    let nx_count = count_nonexistent_children(fs, &child_mods)?;
    if nx_count > 0 {
        let (root_ast, root_child_mods) = walk_file(fs, file_path, true)?;
        let root_nx_count = count_nonexistent_children(fs, &root_child_mods)?;
        if root_nx_count < nx_count {
            return Ok((root_ast, root_child_mods, true));
        }
    }

    Ok((ast, child_mods, false))
}

fn collect_crates_fs(
    fs: &mut impl Filesystem,
    paths: Vec<PathBuf>,
) -> Result<Vec<CrateInfo>, Error> {
    // Visit each file in arbitrary order, and for each one, record all other module files it
    // refers to.  We also record the reverse edges from child to parent, which we use to detect
    // crate roots (modules with no parent).  We visit each file in `paths` as well as any
    // additional files that are discovered during the traversal.
    let mut pending = paths;
    struct WalkedMod {
        inner_end_pos: usize,
        is_root: bool,
        child_mods: Vec<ChildMod>,
    }
    let mut walked = HashMap::<PathBuf, WalkedMod>::new();
    // Map from each .rs file to .rs files that include it as a submodule.
    let mut parents = HashMap::<PathBuf, Vec<PathBuf>>::new();

    while let Some(path) = pending.pop() {
        if walked.contains_key(&path) {
            continue;
        }

        let (ast, child_mods, is_root) = walk_file_guess_root(fs, &path)?;

        for cm in &child_mods {
            if cm.is_inline {
                continue;
            }
            parents.entry(cm.file_path.clone()).or_insert_with(Vec::new).push(path.clone());
            pending.push(cm.file_path.clone());
        }

        walked.insert(path.clone(), WalkedMod {
            inner_end_pos: ast.span().byte_range().end as usize,
            is_root,
            child_mods,
        });
    }

    // Identify roots, which are files that were visited but have no parents.
    let mut roots = walked.keys().filter(|&path| {
        !parents.contains_key(path)
    }).cloned().collect::<Vec<_>>();
    roots.sort();

    // Collect up the modules for each crate.
    let mut crates = Vec::with_capacity(roots.len());
    let mut pending = Vec::<(PathBuf, Vec<String>)>::new();
    // We keep track of every file that's been emitted anywhere in `crates` and ensure we visit
    // each file only once.  This prevents exponential behavior in some edge cases where the same
    // file is referenced more than once.  Note that we don't consider `#[cfg]` attrs during this
    // traversal, so `#[cfg(foo)] mod m; #[cfg(bar)] mod m;` counts as two references to `m.rs`.
    let mut seen = HashSet::new();
    for root in roots {
        let mut modules = Vec::new();
        debug_assert_eq!(pending.len(), 0);
        pending.push((root.clone(), Vec::new()));
        while let Some((file_path, mod_path)) = pending.pop() {
            if seen.contains(&file_path) {
                continue;
            }
            seen.insert(file_path.clone());

            let Some(wm) = walked.remove(&file_path) else { continue };

            let mut child_mods = wm.child_mods;
            if wm.is_root && file_path != root {
                let (_, new_child_mods) = walk_file(fs, file_path.clone(), false)?;
                child_mods = new_child_mods;
            }

            for cm in child_mods {
                let child_mod_path = mod_path.iter().cloned()
                    .chain(cm.rel_mod_path.iter().cloned())
                    .collect::<Vec<_>>();

                if !cm.is_inline {
                    pending.push((cm.file_path.clone(), child_mod_path.clone()));
                }

                modules.push(ModInfo {
                    mod_path: child_mod_path,
                    file_path: cm.file_path,
                    inner_end_pos: cm.inner_end_pos,
                    is_inline: cm.is_inline,
                });
            }
        }

        crates.push(CrateInfo {
            root_file_path: root.clone(),
            modules,
        });
    }

    debug_assert_eq!(walked.len(), 0);

    Ok(crates)
}


#[cfg(test)]
mod test {
    use super::*;

    struct TestFilesystem {
        m: HashMap<String, String>,
    }

    impl Filesystem for TestFilesystem {
        fn read_to_string(&self, path: &Path) -> Result<String, Error> {
            let path = path.to_str().unwrap();
            self.m.get(path).cloned()
                .ok_or_else(|| format!("file not found"))?
        }
        fn exists(&self, path: &Path) -> Result<bool, Error> {
            let path = path.to_str().unwrap();
            self.m.contains_key(path)
        }
    }
}
