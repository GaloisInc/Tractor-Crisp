#![allow(unused)]

// Pointers materialized from integers via safe std calls; no cast is charged.
fn via_expose(addr: usize) -> i32 {
    unsafe { *std::ptr::with_exposed_provenance::<i32>(addr) }
}
fn via_expose_mut(addr: usize) {
    unsafe { *std::ptr::with_exposed_provenance_mut::<i32>(addr) = 1 }
}
fn via_without(addr: usize) -> i32 {
    unsafe { *std::ptr::without_provenance::<i32>(addr) }
}
fn via_without_mut(addr: usize) {
    unsafe { *std::ptr::without_provenance_mut::<i32>(addr) = 2 }
}
fn via_with_addr(base: *const i32, addr: usize) -> i32 {
    unsafe { *base.with_addr(addr) }
}
fn via_map_addr(base: *const i32, addr: usize) -> i32 {
    unsafe { *base.map_addr(|_| addr) }
}
