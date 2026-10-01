#![allow(unused)]

pub struct State { pub value: u32 }

/// Writes through the box after construction.
pub fn make() -> Box<State> {
    let mut state = Box::new(State { value: 0 });
    state.value = 1;
    state
}

pub struct Build { pub offsets: Vec<Option<usize>>, pub strings: Box<[i8]> }

/// Indexes a boxed slice from inside a closure.
pub fn addr_of_string(build: &mut Build, index: usize) -> Option<usize> {
    build.offsets[index].and_then(|offset| {
        build.strings.get_mut(offset).map(|c| c as *mut i8 as usize)
    })
}
