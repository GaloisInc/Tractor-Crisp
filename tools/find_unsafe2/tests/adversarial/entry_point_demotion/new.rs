#![allow(unused)]

/// Export attribute was removed, but the function is otherwise unchanged.
unsafe extern "C" fn adv_demote(p: *const i32) -> i32 {
    unsafe { *p }
}
