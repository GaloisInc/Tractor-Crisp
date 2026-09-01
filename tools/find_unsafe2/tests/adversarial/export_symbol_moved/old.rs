#![allow(unused)]

#[unsafe(export_name = "adv_process")]
unsafe extern "C" fn process_v1(p: *const i32) -> i32 {
    unsafe { *p }
}
