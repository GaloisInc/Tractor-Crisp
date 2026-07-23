#![allow(unused)]

// Deleting an exported static breaks the ABI and frees a unit of headroom.
fn keep() -> i32 {
    0
}
