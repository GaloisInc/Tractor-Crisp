#![allow(unused)]

struct State {
    p: *mut i32,
}
unsafe fn touch(s: &mut State) {
    unsafe { *s.p = 1; }
}
