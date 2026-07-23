#![allow(unused)]

// New laundering helper; global count drops 3 -> 1 but must still be rejected.
fn promote<'a>(p: *const i32) -> &'a i32 {
    unsafe { &*p }
}

fn read1(p: *const i32) -> i32 {
    *promote(p)
}
fn read2(p: *const i32) -> i32 {
    *promote(p)
}
fn read3(p: *const i32) -> i32 {
    *promote(p)
}
