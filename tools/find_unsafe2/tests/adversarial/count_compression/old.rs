#![allow(unused)]

fn sum4(p: *const i32) -> i32 {
    unsafe { *p + *p.wrapping_add(1) + *p.wrapping_add(2) + *p.wrapping_add(3) }
}
