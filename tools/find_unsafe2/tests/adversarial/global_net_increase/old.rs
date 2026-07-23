#![allow(unused)]

fn read2(p: *const i32) -> i32 {
    unsafe { *p }
}
