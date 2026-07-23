#![allow(unused)]

/// Function was renamed, but everything else (including `export_name`) is the same.
#[unsafe(export_name = "adv_process")]
unsafe extern "C" fn process_v2(p: *const i32) -> i32 {
    unsafe { *p }
}
