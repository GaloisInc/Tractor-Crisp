#![allow(unused)]

// Same exported symbol carried by a renamed Rust item: the ABI is intact.
#[unsafe(export_name = "adv_process")]
unsafe extern "C" fn process_v2(p: *const i32) -> i32 {
    unsafe { *p }
}
