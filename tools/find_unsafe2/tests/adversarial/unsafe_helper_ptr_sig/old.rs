#![allow(unused)]

struct State {
    p: *mut i32,
    len: usize,
}
unsafe fn process(s: &mut State) -> i32 {
    let mut sum = 0;
    for i in 0..s.len {
        sum += unsafe { *s.p.add(i) };
    }
    let last = unsafe { *s.p.add(s.len - 1) };
    sum + last
}
