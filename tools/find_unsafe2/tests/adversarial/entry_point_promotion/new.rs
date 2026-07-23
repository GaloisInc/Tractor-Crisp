#![allow(unused)]

// Promotion to entry point would move this fn's unsafety out of the total.
#[unsafe(no_mangle)]
unsafe extern "C" fn adv_promote(p: *const i32) -> i32 {
    unsafe { *p }
}
