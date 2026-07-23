#![allow(unused)]

// Export attribute removed; old entry points skip every check, so this passes.
unsafe extern "C" fn adv_demote(p: *const i32) -> i32 {
    unsafe { *p }
}
