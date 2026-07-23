#![allow(unused)]

// If C still writes this symbol, dropping `mut` is unsound; reads as progress.
#[unsafe(no_mangle)]
pub static ADV_MODE: i32 = 0;
