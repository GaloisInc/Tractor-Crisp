#![allow(unused)]

// The per-function increase is tolerable, but nothing offsets it globally.
fn read2(p: *const i32) -> i32 {
    unsafe { *p + *p.wrapping_add(1) }
}
