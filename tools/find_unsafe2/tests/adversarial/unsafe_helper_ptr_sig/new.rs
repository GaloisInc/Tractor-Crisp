#![allow(unused)]

// Decomposition into an unsafe helper that still carries the pointer-laden
// struct: legal under the safe-endpoint rule, since the helper self-counts.
struct State {
    p: *mut i32,
    len: usize,
}
unsafe fn view(s: &mut State) -> &mut [i32] {
    unsafe { core::slice::from_raw_parts_mut(s.p, s.len) }
}
fn sum(items: &[i32]) -> i32 {
    items.iter().sum()
}
unsafe fn process(s: &mut State) -> i32 {
    let items = unsafe { view(s) };
    sum(items)
}
