#![allow(unused)]

// A new raw-pointer field is charged against the type.
struct Handle {
    idx: usize,
    buf: *const u8,
}

fn keep() -> i32 {
    0
}
