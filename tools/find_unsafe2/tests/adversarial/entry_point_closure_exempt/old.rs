#![allow(unused)]

#[unsafe(no_mangle)]
unsafe extern "C" fn adv_cb(p: *const i32) -> i32 {
    unsafe { *p }
}
