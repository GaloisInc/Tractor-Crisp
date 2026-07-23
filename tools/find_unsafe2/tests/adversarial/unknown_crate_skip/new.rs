#![allow(unused)]

// No baseline inventory exists for this project crate.
fn evil(p: *mut i32) {
    unsafe { *p = 0 }
}
