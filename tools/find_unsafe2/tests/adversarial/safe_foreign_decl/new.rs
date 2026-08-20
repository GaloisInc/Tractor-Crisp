#![allow(unused)]

unsafe extern "C" {
    safe fn adv_ext_get(p: *const u8) -> i32;
}

/// Added a call to a `safe` FFI import.
fn call_it(p: *const u8) -> i32 {
    adv_ext_get(p)
}
