#![allow(unused)]

/// The unrelated `log` closure shifts `read` from `{closure#0}` to `{closure#1}`.
#[unsafe(no_mangle)]
fn work(p: *const i32) -> i32 {
    let log = |x: i32| x + 1;
    let read = |q: *const i32| unsafe { *q };
    log(read(p))
}
