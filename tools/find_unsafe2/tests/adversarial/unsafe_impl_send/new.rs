#![allow(unused)]

struct Handle {
    p: *const i32,
}

fn touch() {}

// Unsafe trait impls warn for review but are not rejected.
unsafe impl Send for Handle {}
unsafe impl Sync for Handle {}
