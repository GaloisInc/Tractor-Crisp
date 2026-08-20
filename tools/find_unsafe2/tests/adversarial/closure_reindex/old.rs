#![allow(unused)]

#[unsafe(no_mangle)]
fn work(p: *const i32) -> i32 {
    let read = |q: *const i32| unsafe { *q };
    read(p)
}
