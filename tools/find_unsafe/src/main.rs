use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{self, Read};
use std::path::{self, PathBuf};
use ciborium;
use clap::Parser;
use serde::Serialize;
use syn::{self, Attribute, ExprUnsafe, Ident, ItemFn, ItemStatic, Meta, Path, StaticMutability};
use syn::visit::{self, Visit};

// Include test files to ensure they compile.
#[allow(warnings)]
mod test_funcs;
#[allow(warnings)]
mod test_statics;

fn is_link_attr(attr: &Attribute) -> bool {
    is_link_attr_meta(&attr.meta)
}

fn is_link_attr_meta(meta: &Meta) -> bool {
    match *meta {
        Meta::Path(ref p) => is_link_attr_path(p),
        Meta::List(ref ml) => {
            if ml.path.get_ident().map_or(false, |i| i == "unsafe") {
                let sub_meta = match syn::parse2::<Meta>(ml.tokens.clone()) {
                    Ok(x) => x,
                    Err(_) => return false,
                };
                is_link_attr_meta(&sub_meta)
            } else {
                is_link_attr_path(&ml.path)
            }
        },
        Meta::NameValue(ref mnv) => is_link_attr_path(&mnv.path),
    }
}

fn is_link_attr_path(path: &Path) -> bool {
    match path.get_ident() {
        Some(i) => {
            i == "no_mangle" || i == "export_name"
        },
        None => false,
    }
}

/// Returns `true` if `f` is exported to other compilation units using a link attribute such as
/// `#[no_mangle]`.
fn fn_is_exported(f: &ItemFn) -> bool {
    f.attrs.iter().any(is_link_attr)
}


#[derive(Clone, Debug, Default)]
#[derive(Serialize)]
struct Output {
    /// Functions that are not accessible from other compilation units and are also unsafe.
    internal_unsafe_fns: Vec<String>,
    /// Functions that contain an unsafe block.
    fns_containing_unsafe: HashSet<String>,
    /// Static that contain an unsafe block.
    statics_containing_unsafe: HashSet<String>,
    /// Static that are mutable, regardless of unsafe.
    mutable_statics: HashSet<String>,
}

#[derive(Clone, Debug)]
enum ItemKind<'a> {
    Fn(&'a Ident),
    Static(&'a Ident),
}

#[derive(Clone, Debug, Default)]
struct Visitor<'a> {
    out: Output,
    current_item: Option<ItemKind<'a>>,
}

impl<'ast> Visit<'ast> for Visitor<'ast> {
    fn visit_item_fn(&mut self, item_fn: &'ast ItemFn) {
        let ident = &item_fn.sig.ident;
        let name = ident.to_string();
        if item_fn.sig.unsafety.is_some() {
            if fn_is_exported(item_fn) {
                // Ignore unsafety inside of FFI entry points, as it's often unavoidable.
                return;
            } else {
                self.out.internal_unsafe_fns.push(name.clone());
            }
        }

        let old = self.current_item.replace(ItemKind::Fn(ident));
        visit::visit_item_fn(self, item_fn);
        self.current_item = old;
    }

    fn visit_item_static(&mut self, item_static: &'ast ItemStatic) {
        let ident = &item_static.ident;
        let name = ident.to_string();
        if matches!(item_static.mutability, StaticMutability::Mut(_)) {
            self.out.mutable_statics.insert(name.clone());
        }

        let old = self.current_item.replace(ItemKind::Static(ident));
        visit::visit_item_static(self, item_static);
        self.current_item = old;
    }

    fn visit_expr_unsafe(&mut self, x: &'ast ExprUnsafe) {
        match self.current_item {
            Some(ItemKind::Fn(ident)) => self.out.fns_containing_unsafe.insert(ident.to_string()),
            Some(ItemKind::Static(ident)) => self.out.statics_containing_unsafe.insert(ident.to_string()),
            None => <_>::default(),
        };
        visit::visit_expr_unsafe(self, x);
    }
}


#[derive(Parser, Debug)]
#[group(multiple = false, required = true)]
struct Args {
    /// Read a single file from stdin and report any unsafe code it contains.
    #[clap(long)]
    stdin: bool,

    /// Read a CBOR object from stdin.  It should contain a dictionary mapping file names to file
    /// contents.
    ///
    /// This is mainly intended for use from CRISP, so it can pass multiple files over stdin rather
    /// than creating temporary files.
    #[clap(long)]
    stdin_cbor: bool,

    /// Read a single file from the given path.
    #[clap(long)]
    file: Option<PathBuf>,

    /// Read all files within a directory (recursively) and report on all of them.
    #[clap(long)]
    dir: Option<PathBuf>,
}

fn read_stdin() -> io::Result<HashMap<PathBuf, String>> {
    let mut src = String::new();
    io::stdin().read_to_string(&mut src)?;
    Ok(HashMap::from([
        (PathBuf::from("input.rs"), src),
    ]))
}

fn read_stdin_cbor() -> Result<HashMap<PathBuf, String>, String> {
    ciborium::from_reader(io::stdin())
        .map_err(|e| e.to_string())
}

fn read_file(path: &path::Path) -> io::Result<HashMap<PathBuf, String>> {
    let mut m = HashMap::new();
    read_file_into(path, &mut m)?;
    Ok(m)
}

fn read_file_into(
    path: &path::Path,
    dest: &mut HashMap<PathBuf, String>,
) -> io::Result<()> {
    let src = fs::read_to_string(path)?;
    let old = dest.insert(path.to_owned(), src);
    assert!(old.is_none(), "duplicate entry for {:?}", path);
    Ok(())
}

fn read_dir(path: &path::Path) -> io::Result<HashMap<PathBuf, String>> {
    let mut m = HashMap::new();
    read_dir_into(path, &mut m)?;
    Ok(m)
}

fn read_dir_into(
    path: &path::Path,
    dest: &mut HashMap<PathBuf, String>,
) -> io::Result<()> {
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            read_dir_into(&entry.path(), dest)?;
        } else {
            if let Some(name) = entry.file_name().to_str() {
                if name.ends_with(".rs") && !name.starts_with('.') {
                    read_file_into(&entry.path(), dest)?;
                }
            }
        }
    }
    Ok(())
}

fn read_files(args: &Args) -> Result<HashMap<PathBuf, String>, String> {
    if args.stdin {
        read_stdin().map_err(|e| e.to_string())
    } else if args.stdin_cbor {
        read_stdin_cbor()
    } else if let Some(ref file) = args.file {
        read_file(file).map_err(|e| e.to_string())
    } else if let Some(ref dir) = args.dir {
        read_dir(dir).map_err(|e| e.to_string())
    } else {
        panic!("must pass at least one input option")
    }
}

fn main() {
    let args = Args::parse();

    let files = read_files(&args).unwrap();

    let mut outputs = HashMap::new();
    for (file_name, src) in files {
        let ast = syn::parse_file(&src).unwrap();

        let mut v = Visitor::default();
        v.visit_file(&ast);
        outputs.insert(file_name, v.out);
    }

    serde_json::to_writer(io::stdout(), &outputs).unwrap();
    println!();
}


#[cfg(test)]
mod tests {
    use super::*;
    use syn::parse_quote;

    #[test]
    fn test_funcs() {
        let file = include_str!("test_funcs.rs");
        let ast = syn::parse_str(file).unwrap();

        let mut v = Visitor::default();
        v.visit_file(&ast);
        let Output {
            internal_unsafe_fns,
            fns_containing_unsafe,
            ..
        } = v.out;

        assert_eq!(internal_unsafe_fns, [
            "f",
        ]);

        assert_eq!(fns_containing_unsafe, [
            "a",
            "c",
            "f",
            "d",
            "g",
        ].into_iter().map(String::from).collect());
    }

    #[test]
    fn test_statics() {
        let file = include_str!("test_statics.rs");
        let ast = syn::parse_str(file).unwrap();

        let mut v = Visitor::default();
        v.visit_file(&ast);
        let Output {
            statics_containing_unsafe,
            mutable_statics,
            ..
        } = v.out;

        assert_eq!(statics_containing_unsafe, [
            "A",
            "C",
            "D",
            "F",
            "_F"
        ].into_iter().map(String::from).collect());

        assert_eq!(mutable_statics, [
            "_A",
            "_C",
            "_D",
            "_F",
        ].into_iter().map(String::from).collect());
    }

    #[test]
    fn test_is_link_attr_no_mangle() {
        let attr: Attribute = parse_quote!(#[no_mangle]);
        assert!(is_link_attr(&attr));
    }

    #[test]
    fn test_is_link_attr_export_name() {
        let attr: Attribute = parse_quote!(#[export_name = "some_name"]);
        assert!(is_link_attr(&attr));
    }

    #[test]
    fn test_is_link_attr_unsafe() {
        let attr: Attribute = parse_quote!(#[unsafe(export_name = "some_name")]);
        assert!(is_link_attr(&attr));
    }

    #[test]
    fn test_is_link_attr_invalid() {
        let attr: Attribute = parse_quote!(#[some_other_attr]);
        assert!(!is_link_attr(&attr));
    }
}
