#![allow(unused)]

// Contract stripped: decl declared safe, call leaves unsafe, count drops.
unsafe extern "C" {
    safe fn adv_ext_get(p: *const u8) -> i32;
}

fn call_it(p: *const u8) -> i32 {
    adv_ext_get(p)
}
