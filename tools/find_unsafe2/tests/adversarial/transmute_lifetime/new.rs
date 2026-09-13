#![allow(unused)]

fn nothing() {}

/// Extends a borrow's lifetime.  MIR optimizations delete this transmute since only lifetimes
/// change, so the drivers turn them off.
fn extend<'a>(x: &'a i32) -> &'static i32 {
    unsafe { std::mem::transmute(x) }
}

/// Changes mutability; survives optimization as a `Transmute` cast.
fn mutate(x: &i32) -> &mut i32 {
    unsafe { std::mem::transmute(&raw const *x) }
}

/// Charged only as a raw pointer deref.  UB checks would add alignment and null checks, which
/// transmute the pointer to `usize`.
fn deref(x: &i32) -> i32 {
    let p = &raw const *x;
    unsafe { *p }
}
