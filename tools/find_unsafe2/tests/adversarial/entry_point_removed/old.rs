#![allow(unused)]

#[unsafe(no_mangle)]
unsafe extern "C" fn adv_removed(p: *const i32) -> i32 {
    unsafe { *p }
}

fn keep() -> i32 {
    1
}
