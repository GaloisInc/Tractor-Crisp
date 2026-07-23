#![allow(unused)]

// Identical body, new name: reads as a brand-new unsafe function.
fn frob2(p: *const i32) -> i32 {
    unsafe { *p }
}
