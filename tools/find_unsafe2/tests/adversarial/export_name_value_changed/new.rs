#![allow(unused)]

// The exported symbol changed: an ABI break.
#[unsafe(export_name = "adv_deflate_v2")]
unsafe extern "C" fn deflate_impl(p: *const u8) -> i32 {
    unsafe { *p as i32 }
}
