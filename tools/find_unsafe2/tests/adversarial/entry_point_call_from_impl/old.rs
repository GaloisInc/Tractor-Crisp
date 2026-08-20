#![allow(unused)]

#[unsafe(no_mangle)]
unsafe extern "C" fn exported_read(p: *const i32) -> i32 {
    unsafe { *p }
}

unsafe fn read_impl(p: *const i32) -> i32 {
    unsafe { *p }
}

fn use_it(p: *const i32) -> i32 {
    unsafe { read_impl(p) }
}
