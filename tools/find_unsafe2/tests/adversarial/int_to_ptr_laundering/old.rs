#![allow(unused)]

fn via_expose(p: *const i32) -> i32 {
    unsafe { *p }
}
fn via_expose_mut(p: *mut i32) {
    unsafe { *p = 1 }
}
fn via_without(p: *const i32) -> i32 {
    unsafe { *p }
}
fn via_without_mut(p: *mut i32) {
    unsafe { *p = 2 }
}
fn via_with_addr(base: *const i32, q: *const i32) -> i32 {
    unsafe { *q }
}
fn via_map_addr(base: *const i32, q: *const i32) -> i32 {
    unsafe { *q }
}
