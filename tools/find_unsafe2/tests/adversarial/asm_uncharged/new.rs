#![allow(unused)]

fn nothing() {}

// Inline asm carries no charge, even in a brand-new function.
#[cfg(target_arch = "x86_64")]
fn spin() {
    unsafe { core::arch::asm!("nop") }
}
