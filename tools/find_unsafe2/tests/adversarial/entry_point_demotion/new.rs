#![allow(unused)]

// Export attribute removed: an ABI break.
unsafe extern "C" fn adv_demote(p: *const i32) -> i32 {
    unsafe { *p }
}
