#![allow(unused)]

// offset_from laundered through integer subtraction; nothing is charged.
fn dist(a: *const u8, b: *const u8) -> isize {
    (b as isize).wrapping_sub(a as isize)
}
