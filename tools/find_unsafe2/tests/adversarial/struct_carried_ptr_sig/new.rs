#![allow(unused)]

struct Inner {
    p: *const i32,
}
struct State {
    inner: Inner,
}
/// Unsound helper like `as_ref` from `as_ref_helper/new.rs`, but the raw pointer doesn't appear
/// directly in the signature.
fn promote<'a, 'b>(s: &'a State) -> &'b i32 {
    unsafe { &*s.inner.p }
}
fn read(s: &State) -> i32 {
    *promote(s)
}
