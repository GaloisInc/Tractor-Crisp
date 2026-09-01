#![allow(unused)]

struct Handle {
    idx: usize,
    /// New raw pointer field was added.
    buf: *const u8,
}

fn keep() -> i32 {
    0
}
