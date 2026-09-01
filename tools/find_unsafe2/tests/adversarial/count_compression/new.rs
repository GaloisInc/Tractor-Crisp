#![allow(unused)]

/// Converted four unsafe derefs into one unsafe call.
fn sum4(p: *const i32) -> i32 {
    let s = unsafe { std::slice::from_raw_parts(p, 4) };
    s[0] + s[1] + s[2] + s[3]
}
