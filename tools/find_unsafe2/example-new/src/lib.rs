#![allow(unused)]

fn f1(x: i32) {
    // `println` internally use `fmt::Arguments::new`, which is an unsafe function.
    eprintln!("Hello, {x}!");
}

unsafe extern "C" {
    fn puts(s: *const u8);
}
fn fake_safe_puts(s: *const u8) {
    unsafe {
        puts(s);
    }
}
fn f2() {
    fake_safe_puts(b"Hello, World!\n\0".as_ptr());
}

fn deref_ptr<T: Copy>(p: *const T) -> T {
    unsafe { *p }
}
fn f3(p: *const i32) -> i32 {
    deref_ptr(p)
}

static mut S: i32 = 123;
fn f4() -> i32 {
    unsafe {
        S += 1;
        S
    }
}

fn f6(r: &i32) -> i32 {
    *r
}
