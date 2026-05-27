#![allow(unused)]

fn f1(x: i32) {
    // `println` internally use `fmt::Arguments::new`, which is an unsafe function.
    eprintln!("Hello, {x}!");
}

unsafe extern "C" {
    fn puts(s: *const u8);
}
fn f2() {
    unsafe {
        puts(b"Hello, World!\n\0".as_ptr());
    }
}

unsafe fn f3(p: *const i32) -> i32 {
    unsafe { *p }
}

static mut S: i32 = 123;
fn f4() -> i32 {
    unsafe {
        S += 1;
        S
    }
}

union U {
    a: i32,
    b: [u8; 4],
}
fn f5a(x: i32) -> i32 {
    // Uses only the `a` field
    unsafe { U { a: x }.a }
}
fn f5b(x: i32) -> [u8; 4] {
    // Uses both `a` and `b` fields
    unsafe { U { a: x }.b }
}

fn f6(r: &i32) -> i32 {
    unsafe { f3(r) }
}

unsafe fn f7(x: *const i32) -> i32 {
    *x
}


#[unsafe(no_mangle)]
unsafe extern "C" fn ffi1(p: *const i32) -> i32 {
    unsafe { *p }
}

#[unsafe(no_mangle)]
static FFI2: i32 = 0;

unsafe extern "C" fn non_ffi3(p: *const i32) -> i32 {
    unsafe { *p }
}


struct S {
    x: i32,
    y: i32,
}
