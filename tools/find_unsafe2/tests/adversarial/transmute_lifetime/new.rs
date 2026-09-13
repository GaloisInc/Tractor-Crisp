#![allow(unused)]

fn nothing() {}

/// Extends a borrow's lifetime.  The optimizer deletes this transmute since only lifetimes
/// change, so it has to be counted on pre-optimization MIR.
fn extend<'a>(x: &'a i32) -> &'static i32 {
    unsafe { std::mem::transmute(x) }
}

/// Changes mutability; survives optimization as a `Transmute` cast.
fn mutate(x: &i32) -> &mut i32 {
    unsafe { std::mem::transmute(&raw const *x) }
}
