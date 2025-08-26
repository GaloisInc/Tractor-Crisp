use std::collections::HashMap;
use std::io::{self, Read};
use std::path;
use ciborium;
use serde::Serialize;
use syn::{self, Attribute, Meta, Path, Ident, Item, ItemFn};
use syn::visit::{self, Visit};


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
            i == "no_mangle" || i == "link_name"
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
}

#[derive(Clone, Debug, Default)]
struct Visitor {
    out: Output,
}

impl<'ast> Visit<'ast> for Visitor {
    fn visit_item_fn(&mut self, item_fn: &'ast ItemFn) {
        if !fn_is_exported(item_fn) && item_fn.sig.unsafety.is_some() {
            self.out.internal_unsafe_fns.push(item_fn.sig.ident.to_string());
        }

        visit::visit_item_fn(self, item_fn);
    }
}


fn read_files_cbor() -> HashMap<path::PathBuf, String> {
    ciborium::from_reader(io::stdin()).unwrap()
}

fn read_files_single_stdin() -> HashMap<path::PathBuf, String> {
    let path = path::PathBuf::from("input.rs");
    let mut src = String::new();
    io::stdin().read_to_string(&mut src).unwrap();
    HashMap::from([
        (path::PathBuf::from("input.rs"), src),
    ])
}

fn main() {
    //let files = read_files_cbor();
    let files = read_files_single_stdin();

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
    fn test_is_link_attr_link_name() {
        let attr: Attribute = parse_quote!(#[link_name = "some_name"]);
        assert!(is_link_attr(&attr));
    }

    #[test]
    fn test_is_link_attr_unsafe() {
        let attr: Attribute = parse_quote!(#[unsafe(link_name = "some_name")]);
        assert!(is_link_attr(&attr));
    }

    #[test]
    fn test_is_link_attr_invalid() {
        let attr: Attribute = parse_quote!(#[some_other_attr]);
        assert!(!is_link_attr(&attr));
    }
}
