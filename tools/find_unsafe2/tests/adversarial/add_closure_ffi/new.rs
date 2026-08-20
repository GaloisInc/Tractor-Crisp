#![allow(unused)]

/// Moves the unsafe deref into a closure.
#[unsafe(no_mangle)]
fn work(p: *const i32) -> i32 {
    let read = |q: *const i32| unsafe { *q };
    read(p)
}
