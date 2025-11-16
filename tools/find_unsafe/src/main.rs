use ciborium;
use clap::Parser;
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::io::{self, Read};
use std::path;
use syn::visit::{self, Visit};
use syn::{self, Attribute, ExprUnsafe, ItemFn, Meta, Path};

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
        }
        Meta::NameValue(ref mnv) => is_link_attr_path(&mnv.path),
    }
}

fn is_link_attr_path(path: &Path) -> bool {
    match path.get_ident() {
        Some(i) => i == "no_mangle" || i == "export_name",
        None => false,
    }
}

/// Returns `true` if `f` is exported to other compilation units using a link attribute such as
/// `#[no_mangle]`.
fn fn_is_exported(f: &ItemFn) -> bool {
    f.attrs.iter().any(is_link_attr)
}

#[derive(Clone, Debug, Default, Serialize)]
struct Output {
    /// Functions that are not accessible from other compilation units and are also unsafe.
    internal_unsafe_fns: Vec<String>,
    /// Functions that contain an unsafe block.
    fns_containing_unsafe: HashSet<String>,
}

#[derive(Clone, Debug, Default)]
struct Visitor {
    out: Output,
    current_fn: Option<String>,
}

impl<'ast> Visit<'ast> for Visitor {
    fn visit_item_fn(&mut self, item_fn: &'ast ItemFn) {
        let name = item_fn.sig.ident.to_string();
        if item_fn.sig.unsafety.is_some() {
            if fn_is_exported(item_fn) {
                // Ignore unsafety inside of FFI entry points, as it's often unavoidable.
                return;
            } else {
                self.out.internal_unsafe_fns.push(name.clone());
            }
        }

        let old = self.current_fn.replace(name);
        visit::visit_item_fn(self, item_fn);
        self.current_fn = old;
    }

    fn visit_expr_unsafe(&mut self, x: &'ast ExprUnsafe) {
        if let Some(ref name) = self.current_fn {
            self.out.fns_containing_unsafe.insert(name.clone());
        }
        visit::visit_expr_unsafe(self, x);
    }
}

#[derive(Parser, Debug)]
struct Args {
    /// Expect the raw contents of a single file on stdin, instead of a CBOR dictionary mapping
    /// file names to file contents.
    #[clap(long)]
    single_file: bool,
}

fn read_files_cbor() -> HashMap<path::PathBuf, String> {
    ciborium::from_reader(io::stdin()).unwrap()
}

fn read_files_single() -> HashMap<path::PathBuf, String> {
    let mut src = String::new();
    io::stdin().read_to_string(&mut src).unwrap();
    HashMap::from([(path::PathBuf::from("input.rs"), src)])
}

fn main() {
    let args = Args::parse();

    let files = if args.single_file {
        read_files_single()
    } else {
        read_files_cbor()
    };

    let mut outputs = HashMap::new();
    for (file_name, src) in files {
        let ast = syn::parse_file(&src).unwrap();

        let mut v = Visitor::default();
        v.visit_file(&ast);
        eprintln!("{:?}", v);
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
