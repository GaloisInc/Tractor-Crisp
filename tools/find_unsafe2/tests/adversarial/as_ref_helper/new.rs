#![allow(unused)]

/// Unsound helper, used to centralize unsafety from multiple call sites.
fn as_ref<'a>(p: *const i32) -> &'a i32 {
    unsafe { &*p }
}

fn read1(p: *const i32) -> i32 {
    *as_ref(p)
}
fn read2(p: *const i32) -> i32 {
    *as_ref(p)
}
fn read3(p: *const i32) -> i32 {
    *as_ref(p)
}
