#![allow(unused)]

pub struct State { pub value: u32 }

pub fn make() -> Box<State> {
    Box::new(State { value: 1 })
}

pub struct Build { pub offsets: Vec<Option<usize>>, pub strings: Box<[i8]> }

pub fn addr_of_string(build: &mut Build, index: usize) -> Option<usize> {
    None
}
