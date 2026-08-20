#![allow(unused)]

/// Same body, but renamed.
fn frob2(p: *const i32) -> i32 {
    unsafe { *p }
}
