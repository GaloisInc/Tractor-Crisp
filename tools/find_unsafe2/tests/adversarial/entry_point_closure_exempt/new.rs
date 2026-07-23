#![allow(unused)]

// The binding closure folds into the exempt entry point.
#[unsafe(no_mangle)]
unsafe extern "C" fn adv_cb(p: *const i32) -> i32 {
    let read = |q: *const i32| unsafe { *q };
    read(p)
}
