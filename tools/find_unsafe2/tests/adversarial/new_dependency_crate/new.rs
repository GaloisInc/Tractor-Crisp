#![allow(unused)]

fn evil(p: *mut i32) {
    unsafe { *p = 0 }
}
