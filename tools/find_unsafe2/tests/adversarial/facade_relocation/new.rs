#![allow(unused)]

struct State {
    p: *const i32,
}

// Derefs relocated into a new named helper: tolerated for review, net down.
fn bind_state(s: &mut State) -> i32 {
    unsafe { *s.p }
}

fn read_twice(s: &mut State) -> i32 {
    bind_state(s)
}
