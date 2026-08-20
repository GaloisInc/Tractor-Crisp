#![allow(unused)]

#[unsafe(no_mangle)]
fn work(p: *const i32) -> i32 {
    unsafe { *p }
}
