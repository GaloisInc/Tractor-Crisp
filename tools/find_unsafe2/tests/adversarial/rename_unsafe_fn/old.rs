#![allow(unused)]

fn frob(p: *const i32) -> i32 {
    unsafe { *p }
}
