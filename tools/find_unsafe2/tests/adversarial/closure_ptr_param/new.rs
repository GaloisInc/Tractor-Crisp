#![allow(unused)]

// A helper closure naming a raw pointer; the fn's own signature is unchanged.
fn scan(p: *const u8, len: usize) -> bool {
    let is_nul = |q: *const u8| unsafe { *q } == 0;
    let mut i = 0;
    while i < len {
        if is_nul(unsafe { p.add(i) }) {
            return true;
        }
        i += 1;
    }
    false
}
