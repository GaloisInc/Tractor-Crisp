#![allow(unused)]

// Deleting an exported static breaks the ABI.
fn keep() -> i32 {
    0
}
