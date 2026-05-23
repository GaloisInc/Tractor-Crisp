use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{self, Read};
use std::path::{self, PathBuf};
use clap::Parser;
use proc_macro2::{TokenStream, TokenTree};
use quote::ToTokens;
use serde::Serialize;
use syn::{
    self, Attribute, ExprUnsafe, ImplItemFn, ItemFn, ItemImpl, ItemMacro, ItemStatic, ItemTrait,
    Macro, Meta, Path, StaticMutability, TraitItemFn,
};
use syn::visit::{self, Visit};

// Include test files to ensure they compile.
#[allow(warnings)]
mod test_funcs;
#[allow(warnings)]
mod test_statics;
#[allow(warnings)]
mod test_macros;

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

/// Returns `true` if the attributes include a link attribute such as `#[no_mangle]` that exports
/// the item to other compilation units.
fn attrs_are_exported(attrs: &[Attribute]) -> bool {
    attrs.iter().any(is_link_attr)
}

fn token_stream_contains_unsafe(tokens: TokenStream) -> bool {
    for token in tokens {
        match token {
            TokenTree::Ident(ident) if ident == "unsafe" => return true,
            TokenTree::Group(group) if token_stream_contains_unsafe(group.stream()) => return true,
            _ => { continue }
        }
    }
    false
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
    /// Macro invocations that contain an unsafe token, and don't belong to an item (ex global invocations).
    global_macro_invocations_containing_unsafe: HashSet<String>,
    /// Macro definitions (macro_rules!) that contain an unsafe token.
    macro_definitions_containing_unsafe: HashSet<String>,
}

#[derive(Clone, Debug)]
enum ItemKind {
    Fn(String),
    Static(String),
}

/// Tracks the enclosing `impl` or `trait` so methods can be reported with a qualified name.
#[derive(Clone, Debug)]
enum MethodScope {
    /// `impl Type { ... }` — methods are reported as `Type::method`.
    Inherent(String),
    /// `impl Trait for Type { ... }` — methods are reported as `<Type as Trait>::method`.
    TraitImpl { self_ty: String, trait_path: String },
    /// `trait Trait { ... }` — default methods are reported as `Trait::method`.
    TraitDef(String),
}

impl MethodScope {
    fn qualify(&self, method: &syn::Ident) -> String {
        match self {
            MethodScope::Inherent(ty) => format!("{}::{}", ty, method),
            MethodScope::TraitImpl { self_ty, trait_path } => {
                format!("<{} as {}>::{}", self_ty, trait_path, method)
            }
            MethodScope::TraitDef(tr) => format!("{}::{}", tr, method),
        }
    }
}

fn type_to_string<T: ToTokens>(ty: &T) -> String {
    ty.to_token_stream().to_string()
}

#[derive(Clone, Debug)]
enum TraversalScope {
    Item(ItemKind),
    Method(MethodScope),
}

#[derive(Clone, Debug, Default)]
struct Visitor {
    out: Output,
    scopes: Vec<TraversalScope>,
}

impl Visitor {
    fn with_scope(&mut self, scope: TraversalScope, visit: impl FnOnce(&mut Self)) {
        self.scopes.push(scope);
        visit(self);
        self.scopes
            .pop()
            .expect("scope pushed immediately before traversal");
    }

    fn current_item(&self) -> Option<&ItemKind> {
        // TODO: Treat `Method` as a boundary when finding the current item. Searching past it
        // preserves the old two-field behavior, but can attribute unsafe in an untracked
        // associated item of a local impl to the enclosing function.
        self.scopes.iter().rev().find_map(|s| match s {
            TraversalScope::Item(i) => Some(i),
            TraversalScope::Method(_) => None,
        })
    }

    fn method_scope(&self) -> Option<&MethodScope> {
        self.scopes.iter().rev().find_map(|s| match s {
            TraversalScope::Item(_) => None,
            TraversalScope::Method(m) => Some(m),
        })
    }
}

impl<'ast> Visit<'ast> for Visitor {
    fn visit_item_fn(&mut self, item_fn: &'ast ItemFn) {
        let name = item_fn.sig.ident.to_string();
        if item_fn.sig.unsafety.is_some() {
            if attrs_are_exported(&item_fn.attrs) {
                // Ignore unsafety inside of FFI entry points, as it's often unavoidable.
                return;
            } else {
                self.out.internal_unsafe_fns.push(name.clone());
            }
        }

        self.with_scope(TraversalScope::Item(ItemKind::Fn(name)), |v| {
            visit::visit_item_fn(v, item_fn)
        });
    }

    fn visit_item_impl(&mut self, item_impl: &'ast ItemImpl) {
        let self_ty = type_to_string(&*item_impl.self_ty);
        let scope = match &item_impl.trait_ {
            Some((_bang, trait_path, _for)) => MethodScope::TraitImpl {
                self_ty,
                trait_path: type_to_string(trait_path),
            },
            None => MethodScope::Inherent(self_ty),
        };
        self.with_scope(TraversalScope::Method(scope), |v| {
            visit::visit_item_impl(v, item_impl)
        });
    }

    fn visit_item_trait(&mut self, item_trait: &'ast ItemTrait) {
        self.with_scope(
            TraversalScope::Method(MethodScope::TraitDef(item_trait.ident.to_string())),
            |v| visit::visit_item_trait(v, item_trait),
        );
    }

    fn visit_impl_item_fn(&mut self, item_fn: &'ast ImplItemFn) {
        let name = match self.method_scope() {
            Some(scope) => scope.qualify(&item_fn.sig.ident),
            None => item_fn.sig.ident.to_string(),
        };
        if item_fn.sig.unsafety.is_some() {
            if attrs_are_exported(&item_fn.attrs) {
                return;
            } else {
                self.out.internal_unsafe_fns.push(name.clone());
            }
        }

        self.with_scope(TraversalScope::Item(ItemKind::Fn(name)), |v| {
            visit::visit_impl_item_fn(v, item_fn)
        });
    }

    fn visit_trait_item_fn(&mut self, item_fn: &'ast TraitItemFn) {
        // Only default-method bodies can contain `unsafe` blocks; signatures without bodies are
        // still tracked for `unsafe fn` reporting.
        let name = match self.method_scope() {
            Some(scope) => scope.qualify(&item_fn.sig.ident),
            None => item_fn.sig.ident.to_string(),
        };
        if item_fn.sig.unsafety.is_some() {
            if attrs_are_exported(&item_fn.attrs) {
                return;
            } else {
                self.out.internal_unsafe_fns.push(name.clone());
            }
        }

        self.with_scope(TraversalScope::Item(ItemKind::Fn(name)), |v| {
            visit::visit_trait_item_fn(v, item_fn)
        });
    }

    fn visit_item_static(&mut self, item_static: &'ast ItemStatic) {
        let name = item_static.ident.to_string();
        if matches!(item_static.mutability, StaticMutability::Mut(_)) {
            self.out.mutable_statics.insert(name.clone());
        }

        self.with_scope(TraversalScope::Item(ItemKind::Static(name)), |v| {
            visit::visit_item_static(v, item_static)
        });
    }

    fn visit_expr_unsafe(&mut self, x: &'ast ExprUnsafe) {
        match self.current_item() {
            Some(ItemKind::Fn(name)) => self.out.fns_containing_unsafe.insert(name.clone()),
            Some(ItemKind::Static(name)) => self.out.statics_containing_unsafe.insert(name.clone()),
            None => <_>::default(),
        };
        visit::visit_expr_unsafe(self, x);
    }

    // This matches both `macro_rules! m { }` definitions as well item macro invocations,
    // (ex. `m!()`). ItemMacro::ident would be `Some(m)` in the first case, and `None`
    // in the second case.
    fn visit_item_macro(&mut self, item_mac: &'ast ItemMacro) {
        let Some(name) = item_mac.ident.as_ref().map(|i| i.to_string()) else {
            // This is an invocation, pass it along.
            visit::visit_item_macro(self, item_mac);
            return;
        };
        
        // This is a macro_rules! definition.
        if token_stream_contains_unsafe(item_mac.mac.tokens.clone()) {
            self.out.macro_definitions_containing_unsafe.insert(name);
        }
    }

    // This matches all macros generically. The only exception is `macro_rules!` definitions,
    // which are intercepted by Self::visit_item_macro and not passed down.
    fn visit_macro(&mut self, mac: &'ast Macro) {
        let Path {leading_colon, segments } = &mac.path;
        let name: String = leading_colon
            .iter()
            .map(|_| Default::default())
            .chain(segments
                .iter()
                .map(|seg| seg.ident.to_string()))
            .collect::<Vec<_>>()
            .join("::");

        if token_stream_contains_unsafe(mac.tokens.clone()) {
            // Attribute unsafe usage in macro invocation to the function we're in, if we can
            match self.current_item() {
                Some(ItemKind::Fn(ident)) | Some(ItemKind::Static(ident)) => self.out.fns_containing_unsafe.insert(ident.clone()),
                None => self.out.global_macro_invocations_containing_unsafe.insert(name),
            };
        }
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
    fn test_macros() {
        let file = include_str!("test_macros.rs");
        let ast = syn::parse_str(file).unwrap();

        let mut v = Visitor::default();
        v.visit_file(&ast);
        let Output {
            global_macro_invocations_containing_unsafe,
            macro_definitions_containing_unsafe,
            fns_containing_unsafe,
            ..
        } = v.out;

        assert_eq!(global_macro_invocations_containing_unsafe, [
            "unsafe_within_invocation2"
       ].into_iter().map(String::from).collect());
       
        assert_eq!(macro_definitions_containing_unsafe, [
            "unsafe_ExprMacro",
            "unsafe_ItemMacro",
            "false_positive",
            "unsafe_StmtMacro",
            "unsafe_TypeMacro"
       ].into_iter().map(String::from).collect());

        assert_eq!(fns_containing_unsafe, [
            "demo"
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
