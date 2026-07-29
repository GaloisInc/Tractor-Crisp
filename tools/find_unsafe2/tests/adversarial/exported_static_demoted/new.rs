#![allow(unused)]

// Dropping the export attribute from a static breaks the ABI.
pub static mut ADV_STATE: i32 = 0;
