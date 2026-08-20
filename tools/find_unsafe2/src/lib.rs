#![feature(rustc_private)]
extern crate rustc_hir;
extern crate rustc_middle;
extern crate rustc_public;

// `rustc_driver` is not used directly, but must be present to avoid "error: crate `rustc_middle`
// required to be available in rlib format, but was not found in this form" when running tests.
extern crate rustc_driver;

use std::collections::{HashMap, HashSet};
use std::hash::Hash;
use std::ops::ControlFlow;
use std::path::Path;
use indexmap::IndexMap;
use rustc_middle::middle::codegen_fn_attrs::CodegenFnAttrFlags;
use rustc_middle::ty::TyCtxt;
use rustc_public::{DefId, CrateDef, CrateDefType, CrateItem, ItemKind};
use rustc_public::mir::{
    Body, Terminator, TerminatorKind, Place, Rvalue, Operand, Safety, FieldIdx, ProjectionElem,
    AggregateKind,
};
use rustc_public::mir::alloc::GlobalAlloc;
use rustc_public::mir::mono::StaticDef;
use rustc_public::rustc_internal;
use rustc_public::ty::{
    Ty, RigidTy, ConstantKind, Prov, FnDef, AdtDef, AdtKind, AliasDef, EarlyBinder, TraitDef,
};
use serde::{Serialize, Deserialize};
use rustc_public::mir::visit::{MirVisitor, PlaceContext, Location};


struct FunctionVisitor<'a> {
    body: &'a Body,
    /// Which statics this function mentions, and how many times for each.  If the function
    /// mentions a `static mut`, we assume it's unsafe, even though `&raw mut S` is actually a safe
    /// operation on its own.
    uses_statics: IndexMap<StaticDef, usize>,
    /// Which functions this function mentions, and how many times for each.  We don't consider
    /// mentions of unsafe or extern functions to be inherently unsafe; we count unsafe calls
    /// explicitly in a separate field.
    uses_fns: IndexMap<FnDef, usize>,
    /// Which struct/union fields this function mentions, and how many times for each.  This
    /// includes both field projections and struct/union literals.  For example, `Struct1(1, 2)` or
    /// `Struct2 { x: 3, y: 4 }` counts as a use of both fields of the struct.  If the function
    /// mentions a union field, we assume it's unsafe, even though creating a union value with a
    /// union literal is actually safe.
    uses_fields: IndexMap<(AdtDef, FieldIdx), usize>,
    /// Number of calls to unsafe functions within the current function.  This includes both direct
    /// calls and indirect calls via function pointers.
    calls_unsafe: usize,
    /// Number of raw pointer dereferences within the current function.
    derefs_raw_ptr: usize,
    /// Number of int-to-pointer casts within the current function.
    casts_int_to_ptr: usize,
    /// Number of inline assembly blocks within the current function.
    inline_asm: usize,
}

impl<'a> FunctionVisitor<'a> {
    pub fn new(body: &'a Body) -> FunctionVisitor<'a> {
        FunctionVisitor {
            body,
            uses_statics: IndexMap::new(),
            uses_fns: IndexMap::new(),
            uses_fields: IndexMap::new(),
            calls_unsafe: 0,
            derefs_raw_ptr: 0,
            casts_int_to_ptr: 0,
            inline_asm: 0,
        }
    }
}

impl MirVisitor for FunctionVisitor<'_> {
    fn visit_place(&mut self, x: &Place, ptx: PlaceContext, loc: Location) {
        let mut ty = self.body.local_decl(x.local).unwrap().ty;
        for proj in &x.projection {
            if let ProjectionElem::Deref = *proj {
                if ty.kind().is_raw_ptr() {
                    self.derefs_raw_ptr += 1;
                }
            } else if let ProjectionElem::Field(idx, _) = *proj {
                if let Some(&RigidTy::Adt(adt, _)) = ty.kind().rigid() {
                    match adt.kind() {
                        AdtKind::Enum => {},
                        AdtKind::Struct | AdtKind::Union => {
                            *self.uses_fields.entry((adt, idx)).or_insert(0) += 1;
                        },
                    }
                }
            }
            ty = proj.ty(ty).unwrap();
        }
        self.super_place(x, ptx, loc);
    }

    fn visit_operand(&mut self, op: &Operand, loc: Location) {
        if let Operand::Constant(ref co) = *op {
            if let ConstantKind::Allocated(ref a) = *co.const_.kind() {
                for &(_, Prov(alloc_id)) in &a.provenance.ptrs {
                    if let GlobalAlloc::Static(sd) = GlobalAlloc::from(alloc_id) {
                        *self.uses_statics.entry(sd).or_insert(0) += 1;
                    }
                }
            }
        }

        match op.ty(self.body.locals()).unwrap().kind().rigid() {
            Some(&RigidTy::FnDef(fd, _)) => {
                *self.uses_fns.entry(fd).or_insert(0) += 1;
            },
            _ => {},
        }

        self.super_operand(op, loc);
    }

    fn visit_rvalue(&mut self, x: &Rvalue, loc: Location) {
        if let Rvalue::Aggregate(ref ag, _) = *x {
            if let AggregateKind::Adt(adt, variant_idx, _, _, union_field_idx) = *ag {
                match adt.kind() {
                    // Don't track enum fields.
                    AdtKind::Enum => {},
                    AdtKind::Struct => {
                        let num_fields = adt.variant(variant_idx).unwrap().fields().len();
                        for idx in 0 .. num_fields {
                            *self.uses_fields.entry((adt, idx)).or_insert(0) += 1;
                        }
                    },
                    AdtKind::Union => {
                        let idx = union_field_idx.unwrap();
                        *self.uses_fields.entry((adt, idx)).or_insert(0) += 1;
                    },
                }
            }
        } else if let Rvalue::Cast(_kind, ref op, dest_ty) = *x {
            if dest_ty.kind().is_raw_ptr() {
                let src_ty = op.ty(self.body.locals()).unwrap();
                if src_ty.kind().is_integral() {
                    self.casts_int_to_ptr += 1;
                }
            }
        }

        self.super_rvalue(x, loc);
    }

    fn visit_terminator(&mut self, x: &Terminator, loc: Location) {
        match x.kind {
            TerminatorKind::Call { ref func, .. } => {
                let ty = func.ty(self.body.locals()).unwrap();
                if let Some(sig) = ty.kind().fn_sig() {
                    if sig.value.safety == Safety::Unsafe {
                        let filename = x.span.get_filename();
                        let is_allowed_unsafe = filename.ends_with("/std/src/macros.rs")
                            || filename.ends_with("/core/src/macros/mod.rs");
                        if !is_allowed_unsafe {
                            self.calls_unsafe += 1;
                        }
                    }
                }
                if let Some(&RigidTy::FnDef(fd, _)) = ty.kind().rigid() {
                    if is_int_to_ptr_call(&fd.name()) {
                        self.casts_int_to_ptr += 1;
                    }
                }
            },
            TerminatorKind::InlineAsm { .. } => {
                self.inline_asm += 1;
            },
            _ => {},
        }

        self.super_terminator(x, loc);
    }
}

/// Calls that convert `usize` to a raw pointer without an `as` cast.  These count toward
/// `casts_int_to_ptr` just like `as` casts do.
fn is_int_to_ptr_call(name: &str) -> bool {
    const INT_TO_PTR_FNS: &[&str] = &[
        "::with_exposed_provenance", "::with_exposed_provenance_mut",
        "::without_provenance", "::without_provenance_mut",
        "::from_exposed_addr", "::from_exposed_addr_mut",
        "::with_addr", "::map_addr",
    ];
    name.contains("::ptr::") && INT_TO_PTR_FNS.iter().any(|s| name.ends_with(s))
}


#[derive(Debug, Serialize, Deserialize, Default)]
pub struct Outputs {
    /// Sum of all unsafe counts from all functions and items, except for FFI entry points.
    ///
    /// This includes only unsafety metrics, not progress metrics.
    pub total_unsafe: usize,

    pub fns: IndexMap<String, FunctionOutputs>,
    pub types: IndexMap<String, TypeOutputs>,

    /// Unsafety: the crate contains unsafe impls.
    ///
    /// These are indexed by parent module so `check_unsafe2` can give more specific errors when
    /// new unsafe impls are added.
    pub unsafe_impls: IndexMap<String, usize>,

    // TODO: Unsafety: crate contains `unsafe extern` imports.
}

#[derive(Debug, Serialize, Deserialize)]
pub struct FunctionOutputs {
    /// Sum of all unsafe counts for this function.
    pub total_unsafe: usize,
    /// Name of the file that contains this function.
    pub filename: String,

    /// Unsafety: the function itself is unsafe.
    ///
    /// It's actually safe to define an `unsafe fn`, but we count this in our unsafety metrics so
    /// the CRISP loop won't stop until all `unsafe fn`s are cleaned up or removed, including ones
    /// that are never called.
    pub is_unsafe_fn: bool,
    /// Unsafety: this "function" is actually a static initializer, and the static is mutable.
    pub is_mut_static: bool,
    /// Unsafety: function dereferences raw pointers.
    pub derefs_raw_ptr: usize,
    /// Unsafety: function calls `unsafe fn`s.
    pub calls_unsafe: usize,
    /// Unsafety: function contains inline assembly, which can access arbitrary memory.
    pub inline_asm: usize,
    /// Unsafety: function mentions `static mut`s.
    ///
    /// This is overapproximated: we count any mention of a static `S` as an access, even though
    /// some mentions, like `&raw mut S`, don't access memory and thus are safe.  This information
    /// is derived from `FunctionVisitor::uses_statics`, which counts all mentions for dependency
    /// tracking purposes.
    pub uses_static_mut: IndexMap<String, usize>,
    /// Unsafety: function mentions union fields.
    ///
    /// This is overapproximated: we treat union construction `U { x: 1 }` as an unsafe use of the
    /// field `U.x`, even though this is a safe operation.  This information is derived from
    /// `FunctionVisitor::uses_fields`, which counts all mentions of each struct or union field for
    /// dependency tracking purposes.
    pub uses_union_field: IndexMap<String, usize>,
    // TODO: Unsafety: function is declared with unsafe attributes.

    /// Progress: function mentions imported `extern` `fn`s.
    pub uses_foreign_fn: IndexMap<String, usize>,
    /// Progress: function mentions local FFI entry points.
    ///
    /// The FFI rules forbid implementation code from calling or referencing exported entry
    /// points, and entry points are exempt from the unsafety checks, so routing work through
    /// them would hide unsafe code.
    pub uses_ffi_entry_point: IndexMap<String, usize>,
    /// Progress: function casts `usize` to a raw pointer.
    ///
    /// This was added after seeing the agent bypass restrictions on raw pointers in data
    /// structures by replacing the pointer with a `usize` and casting back to a pointer at each
    /// use site.  We don't ban reference-to-pointer casts since these may come up naturally when a
    /// function is made mostly safe but it still calls into an unsafe helper.
    pub casts_int_to_ptr: usize,
    /// Progress: function signature contains raw pointers.  This includes every occurrence of
    /// `RigidTy::RawPtr` that appears in the signature, but does not look through type aliases.
    pub sig_contains_raw_ptr: usize,
    // TODO: Progress: function signature type contains raw pointers.

    /// The symbol name of this function, if it's an FFI entry point.  For functions that aren't
    /// FFI entry points (which have neither `#[no_mangle]` nor `#[export_name = "..."]`
    /// attributes), this will be `None`.
    pub ffi_symbol: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct TypeOutputs {
    /// Name of the file that contains this type.
    pub filename: String,

    /// Progress: a field of this type contains a raw pointer.
    ///
    /// For type alias like `type Foo = *const u8;`, we treat the RHS as a field named "type", so
    /// in this case `Foo` would have `field_contains_raw_ptr["field"] == 1`.
    pub field_contains_raw_ptr: IndexMap<String, usize>,
}


impl FunctionOutputs {
    fn calc_total_unsafe(&mut self) {
        let FunctionOutputs {
            ref mut total_unsafe, filename: _,
            is_unsafe_fn, is_mut_static, derefs_raw_ptr, calls_unsafe, inline_asm,
            ref uses_static_mut, ref uses_union_field,
            // Progress, not safety
            uses_ffi_entry_point: _,
            uses_foreign_fn: _, casts_int_to_ptr: _, sig_contains_raw_ptr: _,
            // Other
            ffi_symbol: _,
        } = *self;

        *total_unsafe = is_unsafe_fn as usize
            + is_mut_static as usize
            + derefs_raw_ptr
            + calls_unsafe
            + inline_asm
            + uses_static_mut.values().copied().sum::<usize>()
            + uses_union_field.values().copied().sum::<usize>();
    }

    fn add_from(&mut self, src: &Self) {
        let FunctionOutputs {
            total_unsafe: _, filename: _,
            is_unsafe_fn, is_mut_static,
            derefs_raw_ptr, calls_unsafe, inline_asm,
            ref uses_static_mut, ref uses_union_field,
            // Progress, not safety
            ref uses_ffi_entry_point,
            ref uses_foreign_fn, casts_int_to_ptr, sig_contains_raw_ptr,
            // Other
            ffi_symbol: _,
        } = *src;
        fn add_map<K: Eq + Hash + Clone>(dest: &mut IndexMap<K, usize>, src: &IndexMap<K, usize>) {
            for (k, &v) in src {
                *dest.entry(k.clone()).or_insert(0) += v;
            }
        }
        self.is_unsafe_fn |= is_unsafe_fn;
        self.is_mut_static |= is_mut_static;
        self.derefs_raw_ptr += derefs_raw_ptr;
        self.calls_unsafe += calls_unsafe;
        self.inline_asm += inline_asm;
        add_map(&mut self.uses_static_mut, uses_static_mut);
        add_map(&mut self.uses_union_field, uses_union_field);
        add_map(&mut self.uses_ffi_entry_point, uses_ffi_entry_point);
        add_map(&mut self.uses_foreign_fn, uses_foreign_fn);
        self.casts_int_to_ptr += casts_int_to_ptr;
        self.sig_contains_raw_ptr += sig_contains_raw_ptr;
    }
}

impl TypeOutputs {
    fn total_unsafe(&self) -> usize {
        let TypeOutputs {
            filename: _,
            // Progress, not safety
            field_contains_raw_ptr: _,
        } = *self;

        0
    }
}


/// Whether any item or type of the current crate has its source under `src_dir`.  Used by the
/// drivers to restrict analysis to the project's own crates.  Types are checked too, so a crate
/// holding only type definitions still counts as a project crate.
pub fn any_local_item_under(tcx: TyCtxt, src_dir: &Path) -> bool {
    let all_spans_iter = rustc_public::all_local_items().into_iter().map(|item| item.span())
        .chain(crate_type_defs(tcx).into_iter().map(|td| td.span()));

    // `canonicalize` possibly does a lot of filesystem operations, so check each distinct file
    // path only once.
    let mut files_seen = HashSet::new();
    for span in all_spans_iter {
        let file = span.get_filename();
        if files_seen.insert(file.clone()) {
            if let Ok(file_abs) = Path::new(&file).canonicalize() {
                if file_abs.starts_with(&src_dir) {
                    return true;
                }
            }
        }
    }

    false
}


enum TypeDef {
    Adt(AdtDef),
    Alias(AliasDef, EarlyBinder<Ty>),
}

impl CrateDef for TypeDef {
    fn def_id(&self) -> DefId {
        match *self {
            TypeDef::Adt(adt) => adt.def_id(),
            TypeDef::Alias(ad, _) => ad.def_id(),
        }
    }
}

fn crate_type_defs(tcx: TyCtxt) -> Vec<TypeDef> {
    use rustc_hir::def::DefKind;
    let mut out = Vec::new();
    for item_id in tcx.hir_free_items() {
        let did = item_id.owner_id.to_def_id();
        match tcx.def_kind(did) {
            DefKind::Struct |
            DefKind::Union |
            DefKind::Enum => {
                out.push(TypeDef::Adt(AdtDef(rustc_internal::stable(did))));
            },
            DefKind::TyAlias => {
                let ty = rustc_internal::stable(tcx.type_of(did));
                out.push(TypeDef::Alias(AliasDef(rustc_internal::stable(did)), ty));
            },
            _ => {},
        }
    }
    out
}


fn ty_contains_raw_ptr(ty: Ty) -> usize {
    use rustc_public::visitor::{Visitor, Visitable};
    struct CountRawPtrsVisitor {
        count: usize,
    }
    impl Visitor for CountRawPtrsVisitor {
        type Break = ();
        fn visit_ty(&mut self, ty: &Ty) -> ControlFlow<()> {
            match ty.kind().rigid() {
                Some(&RigidTy::RawPtr(..)) => {
                    self.count += 1;
                },
                Some(&RigidTy::Adt(adt, _)) => {
                    if matches!(&*adt.name(), "core::ptr::NonNull" | "std::ptr::NonNull") {
                        self.count += 1;
                    }
                },
                _ => {},
            }
            ty.super_visit(self)
        }
    }

    let mut v = CountRawPtrsVisitor { count: 0 };
    let _ = ty.visit(&mut v);
    v.count
}

fn sig_contains_raw_ptr(item: CrateItem) -> usize {
    match item.kind() {
        ItemKind::Fn => {
            let sig = item.ty().kind().fn_sig().unwrap();
            sig.value.inputs_and_output.iter().copied().map(ty_contains_raw_ptr).sum()
        },
        ItemKind::Static | ItemKind::Const => ty_contains_raw_ptr(item.ty()),
        ItemKind::Ctor(..) => 0,
    }
}

fn type_def_field_contains_raw_ptr(td: &TypeDef) -> IndexMap<String, usize> {
    match *td {
        TypeDef::Adt(adt) => {
            let mut counts = IndexMap::new();
            for v in adt.variants_iter() {
                for f in v.fields() {
                    // Use `entry` instead of `insert` in case there are multiple enum variants
                    // with the same field name (we don't distinguish between variants currently).
                    *counts.entry(f.name.to_string()).or_insert(0) += ty_contains_raw_ptr(f.ty());
                }
            }
            counts
        },
        // For type aliases, record the count under the dummy field name "type".
        TypeDef::Alias(_, ref ty) => [
            (String::from("type"), ty_contains_raw_ptr(ty.value)),
        ].into(),
    }
}


pub fn process(tcx: TyCtxt) -> Outputs {
    let items = rustc_public::all_local_items();

    let mut is_static_mut = {
        let mut storage = HashMap::new();
        move |sd: StaticDef| -> bool {
            if let Some(&x) = storage.get(&sd) {
                return x;
            }
            let internal_def_id = rustc_internal::internal::<DefId>(tcx, sd.0);
            let x = tcx.is_mutable_static(internal_def_id);
            storage.insert(sd, x);
            x
        }
    };

    let is_adt_union = move |adt: AdtDef| -> bool {
        adt.kind() == AdtKind::Union
    };

    let mut is_fn_foreign = {
        let mut storage = HashMap::new();
        move |fd: FnDef| -> bool {
            if let Some(&x) = storage.get(&fd) {
                return x;
            }
            let internal_def_id = rustc_internal::internal::<DefId>(tcx, fd.0);
            let x = tcx.is_foreign_item(internal_def_id);
            storage.insert(fd, x);
            x
        }
    };

    let mut is_fn_ffi_entry_point = {
        let mut storage = HashMap::new();
        move |fd: FnDef| -> bool {
            if let Some(&x) = storage.get(&fd) {
                return x;
            }
            let x = if CrateItem(fd.0).is_foreign_item() {
                false
            } else {
                let internal_def_id = rustc_internal::internal::<DefId>(tcx, fd.0);
                let attrs = tcx.codegen_fn_attrs(internal_def_id);
                attrs.flags.contains(CodegenFnAttrFlags::NO_MANGLE)
                    || attrs.symbol_name.is_some()
            };
            storage.insert(fd, x);
            x
        }
    };

    let is_unsafe_fn = move |item: CrateItem| {
        if item.kind() != ItemKind::Fn {
            return false;
        }
        let fd = FnDef(item.0);
        fd.fn_sig().value.safety == Safety::Unsafe
    };

    let is_mut_static = move |item: CrateItem| {
        let Ok(sd) = StaticDef::try_from(item) else { return false };
        let internal_def_id = rustc_internal::internal::<DefId>(tcx, sd.0);
        tcx.is_mutable_static(internal_def_id)
    };

    let ffi_symbol = move |item: CrateItem| -> Option<String> {
        if item.is_foreign_item() {
            // FFI imports are not entry points.
            return None;
        }
        if !matches!(item.kind(), ItemKind::Fn /* | ItemKind::Static*/) {
            // Only `fn`s and `static`s can be exported.
            //
            // However, since statics have no inputs (only outputs), we expect they should almost
            // never need unsafe code internally.  So we don't apply the entry-point flag, which
            // allows internal unsafe code.
            //
            // TODO: do set the flag on statics (for accuracy) but filter them out elsewhere
            return None;
        }
        let internal_def_id = rustc_internal::internal::<DefId>(tcx, item.0);
        let attrs = tcx.codegen_fn_attrs(internal_def_id);
        if !attrs.flags.contains(CodegenFnAttrFlags::NO_MANGLE) && attrs.symbol_name.is_none() {
            return None;
        }
        let inst = rustc_middle::ty::Instance::mono(tcx, internal_def_id);
        let sym_name = tcx.symbol_name(inst);
        Some(sym_name.name.to_string())
    };

    // If `item` is a closure, return the `FnDef` of its containing function.
    let closure_parent = |item: CrateItem| -> Option<CrateItem> {
        use rustc_hir::def::DefKind;
        let internal_def_id = rustc_internal::internal::<DefId>(tcx, item.0);
        if !matches!(tcx.def_kind(internal_def_id), DefKind::Closure) {
            return None;
        }
        // Every closure should have a parent, so no need for `tcx.opt_parent` here.
        let parent_internal_def_id = tcx.parent(internal_def_id);
        Some(CrateItem(rustc_internal::stable(parent_internal_def_id)))
    };

    let mut out = Outputs {
        total_unsafe: 0,
        fns: IndexMap::new(),
        types: IndexMap::new(),
        unsafe_impls: IndexMap::new(),
    };

    let mut rollup_map = IndexMap::new();
    for item in items {
        if let Some(body) = item.body() {
            let mut v = FunctionVisitor::new(&body);
            v.visit_body(&body);

            let key: String = item.name();
            let value = FunctionOutputs {
                total_unsafe: 0,    // Calculated later
                filename: item.span().get_filename(),
                is_unsafe_fn: is_unsafe_fn(item),
                is_mut_static: is_mut_static(item),
                derefs_raw_ptr: v.derefs_raw_ptr,
                calls_unsafe: v.calls_unsafe,
                inline_asm: v.inline_asm,
                uses_static_mut: v.uses_statics.iter().filter_map(|(&sd, &count)| {
                    is_static_mut(sd).then(|| (sd.name(), count))
                }).collect(),
                uses_union_field: v.uses_fields.iter().filter_map(|(&(adt, idx), &count)| {
                    is_adt_union(adt).then(|| (format!("{}.{}", adt.name(), idx), count))
                }).collect(),

                uses_foreign_fn: v.uses_fns.iter().filter_map(|(&fd, &count)| {
                    is_fn_foreign(fd).then(|| (fd.name(), count))
                }).collect(),
                uses_ffi_entry_point: v.uses_fns.iter().filter_map(|(&fd, &count)| {
                    is_fn_ffi_entry_point(fd).then(|| (fd.name(), count))
                }).collect(),
                casts_int_to_ptr: v.casts_int_to_ptr,
                sig_contains_raw_ptr: sig_contains_raw_ptr(item),

                ffi_symbol: ffi_symbol(item),
            };
            // Record all closure->parent relationships here.  Additional filtering is applied
            // below when doing the actual folding of closure metrics into parents.
            if let Some(parent) = closure_parent(item) {
                rollup_map.insert(key.clone(), parent.name());
            }
            let old = out.fns.insert(key, value);
            assert!(old.is_none(), "duplicate fns entry for {:?}", item.name());
        }
    }

    for (src, mut dest) in &rollup_map {
        for i in 0.. {
            assert!(i < rollup_map.len(),
                "detected infinite cycle in closure parent graph, involving {dest:?}");
            match rollup_map.get(dest) {
                Some(x) => { dest = x; },
                None => break,
            }
        }

        // Only fold closures into parents if the parent is an FFI entry point.
        let dest_is_ffi = out.fns[dest].ffi_symbol.is_some();
        if dest_is_ffi {
            let src_value = out.fns.swap_remove(src).unwrap();
            let dest_value = out.fns.get_mut(dest).unwrap();
            dest_value.add_from(&src_value);
        }
    }

    for value in out.fns.values_mut() {
        value.calc_total_unsafe();
        if value.ffi_symbol.is_none() {
            out.total_unsafe += value.total_unsafe;
        }
    }

    for td in crate_type_defs(tcx) {
        let key: String = td.name();
        let value = TypeOutputs {
            filename: td.span().get_filename(),
            field_contains_raw_ptr: type_def_field_contains_raw_ptr(&td),
        };
        out.total_unsafe += value.total_unsafe();
        let old = out.types.insert(key, value);
        assert!(old.is_none(), "duplicate types entry for {:?}", td.name());
    }

    for id in rustc_public::local_crate().trait_impls() {
        let it = id.trait_impl();
        let td = it.value.def_id;
        let decl = TraitDef::declaration(&td);
        if matches!(decl.safety, Safety::Unsafe) {
            let impl_parent = id.0.parent().unwrap().name();
            *out.unsafe_impls.entry(impl_parent).or_insert(0) += 1;
            out.total_unsafe += 1;
        }
    }

    out
}
