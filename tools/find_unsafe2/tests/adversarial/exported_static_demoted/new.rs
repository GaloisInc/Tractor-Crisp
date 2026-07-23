#![allow(unused)]

// Dropping the export attribute breaks the ABI; statics carry no export flag.
pub static mut ADV_STATE: i32 = 0;
