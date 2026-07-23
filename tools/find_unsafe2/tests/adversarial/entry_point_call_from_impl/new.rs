#![allow(unused)]

#[unsafe(no_mangle)]
unsafe extern "C" fn exported_read(p: *const i32) -> i32 {
    unsafe { *p }
}

unsafe fn read_impl(p: *const i32) -> i32 {
    unsafe { *p }
}

// Routing through the exempt entry point is charged as a new metric.
fn use_it(p: *const i32) -> i32 {
    unsafe { exported_read(p) }
}
