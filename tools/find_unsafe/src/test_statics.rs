// Mark A
static A: () = {
    unsafe { }
};

// Mark C, not B
static B: () = {
    static C: () = unsafe { };
};

// Mark D, not E
static D: () =
    unsafe { static E: () = { }; };

// Mark F once
static F: () = {
    unsafe { }
    unsafe { unsafe { } }
};

// Mark _A
static mut _A: () = {
};

// Mark _C, not _B
static _B: () = {
    static mut _C: () = { };
};

// Mark D, not E
static mut _D: () = {
    static _E: () = { };
};

// Mark F as both mutable and containing unsafe
static mut _F: () = {
    unsafe { }
    unsafe { }
};
