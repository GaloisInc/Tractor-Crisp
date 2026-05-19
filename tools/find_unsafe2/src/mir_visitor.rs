use rustc_public::mir::{
    Body, Statement, StatementKind, Terminator, TerminatorKind, Place, Rvalue, Operand,
    NonDivergingIntrinsic, CopyNonOverlapping, AssertMessage,
};


pub trait Visitor<'a> {
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

pub fn walk_body<'a, V: Visitor<'a> + ?Sized>(v: &mut V, x: &'a Body) {
    for blk in &x.blocks {
        for stmt in &blk.statements {
            v.visit_statement(stmt);
        }
        v.visit_terminator(&blk.terminator);
    }
}

pub fn walk_statement<'a, V: Visitor<'a> + ?Sized>(v: &mut V, x: &'a Statement) {
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

pub fn walk_terminator<'a, V: Visitor<'a> + ?Sized>(v: &mut V, x: &'a Terminator) {
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

pub fn walk_rvalue<'a, V: Visitor<'a> + ?Sized>(v: &mut V, x: &'a Rvalue) {
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

pub fn walk_operand<'a, V: Visitor<'a> + ?Sized>(v: &mut V, x: &'a Operand) {
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
