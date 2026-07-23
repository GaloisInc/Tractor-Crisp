#![allow(unused)]

unsafe extern "C" {
    fn adv_ext_get(p: *const u8) -> i32;
}

fn call_it(p: *const u8) -> i32 {
    unsafe { adv_ext_get(p) }
}
