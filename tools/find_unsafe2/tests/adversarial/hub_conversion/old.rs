#![allow(unused)]

unsafe fn hub(p: *const i32) -> i32 {
    unsafe { *p }
}

fn caller1(p: *const i32) -> i32 {
    unsafe { *p + *p.wrapping_add(1) }
}
fn caller2(p: *const i32) -> i32 {
    unsafe { *p + *p.wrapping_add(1) }
}
