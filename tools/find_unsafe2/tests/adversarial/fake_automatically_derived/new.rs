#![allow(unused)]

struct Handle {
    p: *const i32,
}

/// The attribute is ordinary syntax; only a real derive expansion is exempt.
#[automatically_derived]
unsafe impl Send for Handle {}
