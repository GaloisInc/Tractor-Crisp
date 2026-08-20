#![allow(unused)]

/// Export attribute was added, but the function is otherwise unchanged.
#[unsafe(no_mangle)]
unsafe extern "C" fn adv_promote(p: *const i32) -> i32 {
    unsafe { *p }
}
