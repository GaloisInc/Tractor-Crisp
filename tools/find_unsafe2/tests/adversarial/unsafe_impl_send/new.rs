#![allow(unused)]

struct Handle {
    p: *const i32,
}

/// Added `unsafe impl`s for an existing type.
unsafe impl Send for Handle {}
unsafe impl Sync for Handle {}
