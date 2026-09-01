#![allow(unused)]

unsafe extern "C" fn adv_promote(p: *const i32) -> i32 {
    unsafe { *p }
}
