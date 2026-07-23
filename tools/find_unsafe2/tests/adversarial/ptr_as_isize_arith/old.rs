#![allow(unused)]

fn dist(a: *const u8, b: *const u8) -> isize {
    unsafe { b.offset_from(a) }
}
