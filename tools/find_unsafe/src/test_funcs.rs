 // Mark a
fn a() {
    unsafe { }
}

// Mark c, not b
fn b() {
    fn c() { unsafe { } };
}

// Mark d, not e
fn d() {
    unsafe { fn e() { } }
}

// Mark f as both containing unsafe and being unsafe fn
unsafe fn f() {
    unsafe { }
}

// Mark g once
fn g() {
    unsafe { }
    unsafe { unsafe { } }
}
