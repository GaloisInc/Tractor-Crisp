#![allow(unused)]

union Bits {
    i: i32,
    f: f32,
}

// Union construction is safe Rust but is still charged as a field use.
fn keep(x: i32) -> Bits {
    Bits { i: x }
}
