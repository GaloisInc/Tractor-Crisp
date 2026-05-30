#![feature(rustc_private)]
extern crate rustc_middle;
extern crate rustc_public;

// `rustc_driver` is not used directly, but must be present to avoid "error: crate `rustc_middle`
// required to be available in rlib format, but was not found in this form" when running tests.
extern crate rustc_driver;

use std::collections::HashMap;
use indexmap::IndexMap;
use rustc_middle::middle::codegen_fn_attrs::CodegenFnAttrFlags;
use rustc_middle::ty::TyCtxt;
use rustc_public::{DefId, CrateDef, CrateItem, ItemKind};
use rustc_public::mir::{
    Body, Terminator, TerminatorKind, Place, Rvalue, Operand, Safety, FieldIdx, ProjectionElem,
    AggregateKind,
};
use rustc_public::mir::alloc::GlobalAlloc;
use rustc_public::mir::mono::StaticDef;
use rustc_public::rustc_internal;
use rustc_public::ty::{RigidTy, ConstantKind, Prov, FnDef, AdtDef, AdtKind};
use serde::{Serialize, Deserialize};
use crate::mir_visitor::Visitor;

mod mir_visitor;


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
        }
    }
}

impl Visitor<'_> for FunctionVisitor<'_> {
    fn visit_place(&mut self, x: &Place) {
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
    }

    fn visit_operand(&mut self, op: &Operand) {
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

        mir_visitor::walk_operand(self, op);
    }

    fn visit_rvalue(&mut self, x: &Rvalue) {
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
        }

        mir_visitor::walk_rvalue(self, x);
    }

    fn visit_terminator(&mut self, x: &Terminator) {
        if let TerminatorKind::Call { ref func, .. } = x.kind {
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
        }

        mir_visitor::walk_terminator(self, x);
    }
}


#[derive(Debug, Serialize, Deserialize)]
pub struct Outputs {
    /// Sum of all unsafe counts from all functions and items, except for FFI entry points.
    ///
    /// This includes only unsafety metrics, not progress metrics.
    pub total_unsafe: usize,

    pub fns: IndexMap<String, FunctionOutputs>,

    // TODO: Unsafety: crate implements `unsafe trait`s.
    // TODO: Unsafety: crate contains `unsafe extern` imports.

    // TODO: Progress: struct field type contains raw pointers.
}

#[derive(Debug, Serialize, Deserialize)]
pub struct FunctionOutputs {
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
    // TODO: Progress: function signature type contains raw pointers.

    /// Whether this function is an FFI entry point.  Specifically, this is set when the function
    /// has the `#[no_mangle]` or `#[export_name = ...]` attribute.
    pub is_ffi_entry_point: bool,
}

impl FunctionOutputs {
    fn total_unsafe(&self) -> usize {
        let FunctionOutputs {
            is_unsafe_fn, is_mut_static, derefs_raw_ptr, calls_unsafe,
            ref uses_static_mut, ref uses_union_field,
            // Progress, not safety
            uses_foreign_fn: _,
            // Other
            is_ffi_entry_point: _,
        } = *self;

        is_unsafe_fn as usize
            + is_mut_static as usize
            + derefs_raw_ptr
            + calls_unsafe
            + uses_static_mut.values().copied().sum::<usize>()
            + uses_union_field.values().copied().sum::<usize>()
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

    let is_ffi_entry_point = move |item: CrateItem| {
        if item.is_foreign_item() {
            // FFI imports are not entry points.
            return false;
        }
        if !matches!(item.kind(), ItemKind::Fn /* | ItemKind::Static*/) {
            // Only `fn`s and `static`s can be exported.
            //
            // However, since statics have no inputs (only outputs), we expect they should almost
            // never need unsafe code internally.  So we don't apply the entry-point flag, which
            // allows internal unsafe code.
            //
            // TODO: do set the flag on statics (for accuracy) but filter them out elsewhere
            return false;
        }
        let internal_def_id = rustc_internal::internal::<DefId>(tcx, item.0);
        let attrs = tcx.codegen_fn_attrs(internal_def_id);
        attrs.flags.contains(CodegenFnAttrFlags::NO_MANGLE)
            || attrs.symbol_name.is_some()
    };

    let mut out = Outputs {
        total_unsafe: 0,
        fns: IndexMap::new(),
    };
    for item in items {
        if let Some(body) = item.body() {
            let mut v = FunctionVisitor::new(&body);
            v.visit_body(&body);

            let key: String = item.name();
            let value = FunctionOutputs {
                is_unsafe_fn: is_unsafe_fn(item),
                is_mut_static: is_mut_static(item),
                derefs_raw_ptr: v.derefs_raw_ptr,
                calls_unsafe: v.calls_unsafe,
                uses_static_mut: v.uses_statics.iter().filter_map(|(&sd, &count)| {
                    is_static_mut(sd).then(|| (sd.name(), count))
                }).collect(),
                uses_union_field: v.uses_fields.iter().filter_map(|(&(adt, idx), &count)| {
                    is_adt_union(adt).then(|| (format!("{}.{}", adt.name(), idx), count))
                }).collect(),

                uses_foreign_fn: v.uses_fns.iter().filter_map(|(&fd, &count)| {
                    is_fn_foreign(fd).then(|| (fd.name(), count))
                }).collect(),

                is_ffi_entry_point: is_ffi_entry_point(item),
            };
            if !value.is_ffi_entry_point {
                out.total_unsafe += value.total_unsafe();
            }
            let old = out.fns.insert(key, value);
            assert!(old.is_none(), "duplicate entry for {:?}", item.name());
        }
    }

    out
}
