#![allow(unused)]

fn scan(p: *const u8, len: usize) -> bool {
    let mut i = 0;
    while i < len {
        if unsafe { *p.add(i) } == 0 {
            return true;
        }
        i += 1;
    }
    false
}
