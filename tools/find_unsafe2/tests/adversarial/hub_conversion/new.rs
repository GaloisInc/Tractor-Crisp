#![allow(unused)]

/// Existing unsafe helper gained more unsafe to reduce unsafety at call sites.
unsafe fn hub(p: *const i32) -> i32 {
    unsafe { *p + *p.wrapping_add(1) }
}

fn caller1(p: *const i32) -> i32 {
    unsafe { hub(p) }
}
fn caller2(p: *const i32) -> i32 {
    unsafe { hub(p) }
}
