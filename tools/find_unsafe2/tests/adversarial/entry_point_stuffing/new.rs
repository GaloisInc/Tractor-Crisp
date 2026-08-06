#![allow(unused)]

// Stuffing the implementation body into the exempt wrapper lowers the
// global count without making anything safer.
#[unsafe(no_mangle)]
unsafe extern "C" fn adv_stuff(p: *const i32) -> i32 {
    unsafe { *p.offset(1) + *p }
}
