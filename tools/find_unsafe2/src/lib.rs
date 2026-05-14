#![feature(rustc_private)]
extern crate rustc_public;

// Required by rustc_public::run! macro
extern crate rustc_driver;
extern crate rustc_interface;
extern crate rustc_middle;

use std::collections::HashMap;
use indexmap::IndexMap;
use rustc_middle::ty::TyCtxt;
use rustc_public::{DefId, CrateDef};
use rustc_public::mir::{
    Body, Statement, StatementKind, Terminator, TerminatorKind, Place, Rvalue, Operand,
    NonDivergingIntrinsic, CopyNonOverlapping, AssertMessage, Safety, FieldIdx, ProjectionElem,
    AggregateKind,
};
use rustc_public::mir::alloc::GlobalAlloc;
use rustc_public::mir::mono::StaticDef;
use rustc_public::rustc_internal;
use rustc_public::ty::{RigidTy, ConstantKind, Prov, FnDef, AdtDef, AdtKind};
use serde::{Serialize, Deserialize};


trait Visitor<'a> {
    fn visit_body(&mut self, x: &'a Body) {
        walk_body(self, x);
    }
    fn visit_statement(&mut self, x: &'a Statement) {
        walk_statement(self, x);
    }
    fn visit_terminator(&mut self, x: &'a Terminator) {
        walk_terminator(self, x);
    }
    fn visit_place(&mut self, x: &'a Place) {
        let _ = x;
    }
    fn visit_rvalue(&mut self, x: &'a Rvalue) {
        walk_rvalue(self, x);
    }
    fn visit_operand(&mut self, x: &'a Operand) {
        walk_operand(self, x);
    }
}

fn walk_body<'a, V: Visitor<'a> + ?Sized>(v: &mut V, x: &'a Body) {
    for blk in &x.blocks {
        for stmt in &blk.statements {
            v.visit_statement(stmt);
        }
        v.visit_terminator(&blk.terminator);
    }
}

fn walk_statement<'a, V: Visitor<'a> + ?Sized>(v: &mut V, x: &'a Statement) {
    match x.kind {
        StatementKind::Assign(ref pl, ref rv) => {
            v.visit_place(pl);
            v.visit_rvalue(rv);
        },
        StatementKind::FakeRead(_, ref pl) => {
            v.visit_place(pl);
        },
        StatementKind::SetDiscriminant { ref place, variant_index: _ } => {
            v.visit_place(place);
        },
        StatementKind::StorageLive(..) => {},
        StatementKind::StorageDead(..) => {},
        StatementKind::PlaceMention(ref pl) => {
            v.visit_place(pl);
        },
        StatementKind::AscribeUserType { ref place, projections: _, variance: _ } => {
            v.visit_place(place);
        },
        StatementKind::Coverage(..) => {},
        StatementKind::Intrinsic(ref intr) => {
            match *intr {
                NonDivergingIntrinsic::Assume(ref op) => {
                    v.visit_operand(op);
                },
                NonDivergingIntrinsic::CopyNonOverlapping(ref cno) => {
                    let CopyNonOverlapping { ref src, ref dst, ref count } = *cno;
                    v.visit_operand(src);
                    v.visit_operand(dst);
                    v.visit_operand(count);
                },
            }
        },
        StatementKind::ConstEvalCounter => {},
        StatementKind::Nop => {},
    }
}

fn walk_terminator<'a, V: Visitor<'a> + ?Sized>(v: &mut V, x: &'a Terminator) {
    match x.kind {
        TerminatorKind::Goto { .. } => {},
        TerminatorKind::SwitchInt { ref discr, .. } => {
            v.visit_operand(discr);
        },
        TerminatorKind::Resume => {},
        TerminatorKind::Abort => {},
        TerminatorKind::Return => {},
        TerminatorKind::Unreachable => {},
        TerminatorKind::Drop { ref place, .. } => {
            v.visit_place(place);
        },
        TerminatorKind::Call { ref func, ref args, ref destination, .. } => {
            v.visit_operand(func);
            for arg in args {
                v.visit_operand(arg);
            }
            v.visit_place(destination);
        },
        TerminatorKind::Assert { ref cond, ref msg, .. } => {
            v.visit_operand(cond);
            match *msg {
                AssertMessage::BoundsCheck { ref len, ref index } => {
                    v.visit_operand(len);
                    v.visit_operand(index);
                },
                AssertMessage::Overflow(_, ref op1, ref op2) => {
                    v.visit_operand(op1);
                    v.visit_operand(op2);
                },
                AssertMessage::OverflowNeg(ref op) => {
                    v.visit_operand(op);
                },
                AssertMessage::DivisionByZero(ref op) => {
                    v.visit_operand(op);
                },
                AssertMessage::RemainderByZero(ref op) => {
                    v.visit_operand(op);
                },
                AssertMessage::ResumedAfterReturn(..) => {},
                AssertMessage::ResumedAfterPanic(..) => {},
                AssertMessage::ResumedAfterDrop(..) => {},
                AssertMessage::MisalignedPointerDereference { ref required, ref found } => {
                    v.visit_operand(required);
                    v.visit_operand(found);
                },
                AssertMessage::NullPointerDereference => {},
                AssertMessage::InvalidEnumConstruction(ref op) => {
                    v.visit_operand(op);
                },
            }
        },
        TerminatorKind::InlineAsm { ref operands, .. } => {
            for operand in operands {
                if let Some(ref in_value) = operand.in_value {
                    v.visit_operand(in_value);
                }
                if let Some(ref out_place) = operand.out_place {
                    v.visit_place(out_place);
                }
            }
        },
    }
}

fn walk_rvalue<'a, V: Visitor<'a> + ?Sized>(v: &mut V, x: &'a Rvalue) {
    match *x {
        Rvalue::AddressOf(_, ref pl) => {
            v.visit_place(pl);
        },
        Rvalue::Aggregate(_, ref ops) => {
            for op in ops {
                v.visit_operand(op);
            }
        },
        Rvalue::BinaryOp(_, ref op1, ref op2) => {
            v.visit_operand(op1);
            v.visit_operand(op2);
        },
        Rvalue::Cast(_, ref op, _) => {
            v.visit_operand(op);
        },
        Rvalue::CheckedBinaryOp(_, ref op1, ref op2) => {
            v.visit_operand(op1);
            v.visit_operand(op2);
        },
        Rvalue::CopyForDeref(ref pl) => {
            v.visit_place(pl);
        },
        Rvalue::Discriminant(ref pl) => {
            v.visit_place(pl);
        },
        Rvalue::Len(ref pl) => {
            v.visit_place(pl);
        },
        Rvalue::Ref(_, _, ref pl) => {
            v.visit_place(pl);
        },
        Rvalue::Repeat(ref op, _) => {
            v.visit_operand(op);
        },
        Rvalue::ThreadLocalRef(..) => {},
        Rvalue::UnaryOp(_, ref op) => {
            v.visit_operand(op);
        },
        Rvalue::Use(ref op, _) => {
            v.visit_operand(op);
        },
        Rvalue::Reborrow(_, _, ref pl) => {
            v.visit_place(pl);
        },
    }
}

fn walk_operand<'a, V: Visitor<'a> + ?Sized>(v: &mut V, x: &'a Operand) {
    match *x {
        Operand::Copy(ref pl) => {
            v.visit_place(pl);
        },
        Operand::Move(ref pl) => {
            v.visit_place(pl);
        },
        Operand::Constant(..) => {},
        Operand::RuntimeChecks(..) => {},
    }
}


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

        walk_operand(self, op);
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

        walk_rvalue(self, x);
    }

    fn visit_terminator(&mut self, x: &Terminator) {
        if let TerminatorKind::Call { ref func, .. } = x.kind {
            let ty = func.ty(self.body.locals()).unwrap();
            if let Some(sig) = ty.kind().fn_sig() {
                if sig.value.safety == Safety::Unsafe {
                    let is_allowed_unsafe = x.span.get_filename().ends_with("/std/src/macros.rs");
                    if !is_allowed_unsafe {
                        self.calls_unsafe += 1;
                    }
                }
            }
        }

        walk_terminator(self, x);
    }
}


#[derive(Debug, Serialize, Deserialize)]
pub struct Outputs {
    pub fns: IndexMap<String, FunctionOutputs>,

    // TODO: Unsafety: crate implements `unsafe trait`s.
    // TODO: Progress: struct field type contains raw pointers
}

#[derive(Debug, Serialize, Deserialize)]
pub struct FunctionOutputs {
    /// Unsafety: function dereferences raw pointers.
    pub derefs_raw_ptr: usize,
    /// Unsafety: function calls `unsafe fn`s.
    pub calls_unsafe: usize,
    /// Unsafety: function mentions `static mut`s.
    pub uses_static_mut: IndexMap<String, usize>,
    /// Unsafety: function mentions union fields.
    pub uses_union_field: IndexMap<String, usize>,

    /// Progress: function mentions imported `extern` `fn`s.
    pub uses_foreign_fn: IndexMap<String, usize>,
    // TODO: Progress: function signature type contains raw pointers
}


pub fn process(tcx: TyCtxt) -> Outputs {
    eprintln!("PROCESS: found crate {:?}", rustc_public::local_crate().name);
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

    let mut out = Outputs {
        fns: IndexMap::new(),
    };
    for item in items {
        eprintln!("item {item:?}");
        if let Some(body) = item.body() {
            let mut v = FunctionVisitor::new(&body);
            v.visit_body(&body);

            let key: String = item.name();
            let value = FunctionOutputs {
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
            };
            let old = out.fns.insert(key, value);
            assert!(old.is_none(), "duplicate entry for {:?}", item.name());
        }
    }

    out
}
