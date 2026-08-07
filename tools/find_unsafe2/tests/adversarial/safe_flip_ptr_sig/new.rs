#![allow(unused)]

// Dropping the `unsafe` qualifier while the signature still carries the
// pointer-laden struct: the flip half of the create-then-flip laundering.
struct State {
    p: *mut i32,
}
fn touch(s: &mut State) {
    let _ = s;
}
