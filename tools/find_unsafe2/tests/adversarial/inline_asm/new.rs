#![allow(unused)]

fn nothing() {}

/// New function uses inline assembly.
#[cfg(target_arch = "x86_64")]
fn spin() {
    unsafe { core::arch::asm!("nop") }
}
