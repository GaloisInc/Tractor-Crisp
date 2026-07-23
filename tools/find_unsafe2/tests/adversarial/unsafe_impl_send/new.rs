#![allow(unused)]

struct Handle {
    p: *const i32,
}

/// Added `unsafe impl`s for an existing type.
unsafe impl Send for Handle {}

mod nested {
    use super::*;

    /// Unsafe impl errors should report the module path.
    unsafe impl Sync for Handle {}
}
