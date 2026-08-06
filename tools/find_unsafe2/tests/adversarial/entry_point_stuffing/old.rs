#![allow(unused)]

#[unsafe(no_mangle)]
unsafe extern "C" fn adv_stuff(p: *const i32) -> i32 {
    unsafe { helper(p) }
}

unsafe fn helper(p: *const i32) -> i32 {
    unsafe { *p.offset(1) + *p }
}
