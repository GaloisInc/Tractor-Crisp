// ExprMacro
macro_rules! unsafe_ExprMacro {
  () => {{
      let x: u8 = unsafe { std::mem::zeroed() };
      x
  }};
}

// ItemMacro
macro_rules! unsafe_ItemMacro {
  () => {
      unsafe fn generated_item_fn() {}
  };
}

// StmtMacro
macro_rules! unsafe_StmtMacro {
  () => {
      let _y: u8 = unsafe { std::mem::zeroed() };
  };
}

// TypeMacro
macro_rules! unsafe_TypeMacro {
  () => {
      unsafe extern "C" fn()
  };
}

// This is safe, but the parameter token is called 'unsafe'
macro_rules! false_positive {
    ($unsafe:tt) => { $unsafe }
}

// Trying to sneak in an unsafe token.
macro_rules! unsafe_within_invocation {
    ($x:tt) => {
        $x { };
    };
}

//
macro_rules! unsafe_within_invocation2 {
    ($x:expr) => { }
}

// ----- Uses -----

unsafe_ItemMacro!();

type Callback = unsafe_TypeMacro!();

unsafe_within_invocation2!({ unsafe { 5 } });

fn demo(v: i32) {
    let _a = unsafe_ExprMacro!();

    unsafe_StmtMacro!();

    unsafe_within_invocation!(unsafe);

    unsafe_within_invocation2!({ unsafe { 5 } });

    false_positive!(5);
}
