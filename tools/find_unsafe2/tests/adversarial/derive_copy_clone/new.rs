#![allow(unused)]

/// `derive(Copy, Clone)` expands to `unsafe impl TrivialClone`, which must not be charged.
#[derive(Copy, Clone)]
pub struct State { pub value: u32 }
