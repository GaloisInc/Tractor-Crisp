#![allow(unused)]

// Stands in for a dependency: unsafe code compiled from outside `SRC_DIR`.
fn evil(p: *mut i32) {
    unsafe { *p = 0 }
}
