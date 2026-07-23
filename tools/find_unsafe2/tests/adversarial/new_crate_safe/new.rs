#![allow(unused)]

// No baseline inventory exists for this project crate, but it adds no unsafety.
fn clamp(x: i32, lo: i32, hi: i32) -> i32 {
    if x < lo { lo } else if x > hi { hi } else { x }
}

struct Range {
    lo: i32,
    hi: i32,
}
