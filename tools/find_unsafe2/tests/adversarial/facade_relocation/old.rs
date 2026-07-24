#![allow(unused)]

struct State {
    p: *const i32,
}

fn read_twice(s: &mut State) -> i32 {
    unsafe { *s.p + *s.p.wrapping_add(1) }
}
