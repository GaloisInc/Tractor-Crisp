#![allow(unused)]

struct Inner {
    p: *const i32,
}
struct State {
    inner: Inner,
}
fn read(s: &State) -> i32 {
    unsafe { *s.inner.p }
}
