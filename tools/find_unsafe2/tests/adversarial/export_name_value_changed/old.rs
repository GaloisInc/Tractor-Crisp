#![allow(unused)]

#[unsafe(export_name = "adv_deflate")]
unsafe extern "C" fn deflate_impl(p: *const u8) -> i32 {
    unsafe { *p as i32 }
}
