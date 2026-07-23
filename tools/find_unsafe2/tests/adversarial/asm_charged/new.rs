#![allow(unused)]

fn nothing() {}

// Inline asm is charged, even in a brand-new function.
fn spin() {
    unsafe { core::arch::asm!("") }
}
