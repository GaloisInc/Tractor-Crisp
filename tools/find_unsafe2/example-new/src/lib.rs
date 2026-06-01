#![allow(unused)]
use std::ptr::NonNull;

fn f1(x: i32) {
    // `println` internally use `fmt::Arguments::new`, which is an unsafe function.
    eprintln!("Hello, {x}!");
}

unsafe extern "C" {
    fn puts(s: *const u8);
}
fn fake_safe_puts(s: *const u8) {
    // Error: introduced a new function containing unsafe.
    unsafe {
        puts(s);
    }
}
fn f2() {
    fake_safe_puts(b"Hello, World!\n\0".as_ptr());
}

fn deref_ptr<T: Copy>(p: *const T) -> T {
    // Error: introduced a new function containing unsafe.
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

unsafe fn f7a(x: usize) -> i32 {
    // Error: integer-to-pointer casts are forbidden.
    *(x as *const i32)
}

unsafe fn f7b(x: &i32) -> i32 {
    // No error; reference-to-pointer casts are allowed.
    *(x as *const i32)
}


#[unsafe(no_mangle)]
unsafe extern "C" fn ffi1(p: *const i32) -> i32 {
    // Added a second pointer dereference, but no error because this is an FFI entry point.
    unsafe { *p + *p }
}

#[unsafe(no_mangle)]
static FFI2: i32 = {
    // Added a new raw pointer dereference.  Even though this static is an FFI entry point, we
    // still disallow unsafe operations in entry-point statics, as described in a comment in
    // `is_ffi_entry_point`.
    let x = 0;
    let p = &raw const x;
    unsafe { *p }
};

// Error: converted non-FFI function into FFI entry point.
#[unsafe(no_mangle)]
unsafe extern "C" fn non_ffi3(p: *const i32) -> i32 {
    unsafe { *p }
}


struct S {
    // Error: added a raw pointer to this field type.
    x: *const i32,
    // Error - NonNull also counts as a raw pointer type.
    y: NonNull<i32>,
    // Error - NonNull is detected even underneath `Option`.
    z: Option<NonNull<i32>>,
}


fn test_write() {
    use std::io::Write;
    // `writeln!` uses unsafe `std::fmt` internals, but shouldn't be counted as unsafe.
    let mut stdout = std::io::stdout().lock();
    let _ = writeln!(stdout, "x = {}", 1 + 1);
}
