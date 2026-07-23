#![allow(unused)]

struct Handle {
    p: *const i32,
}

// Unsafe trait impls carry no charge.
unsafe impl Send for Handle {}
unsafe impl Sync for Handle {}
