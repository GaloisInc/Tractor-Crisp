#![allow(unused)]

// A lifetime-forging helper whose signature hides the pointer inside a struct.
struct Inner {
    p: *const i32,
}
struct State {
    inner: Inner,
}
fn promote<'a, 'b>(s: &'a State) -> &'b i32 {
    unsafe { &*s.inner.p }
}
fn read(s: &State) -> i32 {
    *promote(s)
}
