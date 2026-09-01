#![allow(unused)]

#[unsafe(no_mangle)]
pub static mut ADV_TABLE: [i32; 4] = [1, 2, 3, 4];

fn keep() -> i32 {
    0
}
