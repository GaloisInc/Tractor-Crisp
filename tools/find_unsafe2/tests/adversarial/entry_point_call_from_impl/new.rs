#![allow(unused)]

#[unsafe(no_mangle)]
unsafe extern "C" fn exported_read(p: *const i32) -> i32 {
    unsafe { *p }
}

unsafe fn read_impl(p: *const i32) -> i32 {
    unsafe { *p }
}

/// Calls the FFI entry point `exported_read` directly, instead of `read_impl`.
fn use_it(p: *const i32) -> i32 {
    unsafe { exported_read(p) }
}
