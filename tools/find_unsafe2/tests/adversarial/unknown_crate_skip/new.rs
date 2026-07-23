#![allow(unused)]

// No baseline JSON exists for this crate; the check skips it entirely.
fn evil(p: *mut i32) {
    unsafe { *p = 0 }
}
