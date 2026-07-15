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

struct H;

// Inherent impl: mark `H::h` as containing unsafe.
impl H {
    fn h(&self) {
        unsafe { }
    }

    // Mark `H::i` both as containing unsafe and as an unsafe fn.
    unsafe fn i(&self) {
        unsafe { }
    }
}

trait J {
    fn j(&self);

    // Default trait method body: mark `J::k` as containing unsafe.
    fn k(&self) {
        unsafe { }
    }
}

// Trait impl: mark `<H as J>::j` as containing unsafe.
impl J for H {
    fn j(&self) {
        unsafe { }
    }
}
