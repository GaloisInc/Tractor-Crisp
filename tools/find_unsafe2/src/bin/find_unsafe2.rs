#![feature(rustc_private)]
extern crate rustc_public;

// Required by rustc_public::run! macro
extern crate rustc_driver;
extern crate rustc_interface;
extern crate rustc_middle;

use std::collections::HashSet;
use std::env;
use std::fs::{self, File};
use std::path::Path;
use rustc_public::error::CompilerError;
use serde_json;
use find_unsafe2;


fn main() {
    let src_dir = env::var("FIND_UNSAFE2_SRC_DIR").unwrap();
    let src_dir = Path::new(&src_dir);
    assert!(src_dir.is_absolute(),
        "expected $FIND_UNSAFE2_SRC_DIR to be an absolute path, but got {:?}", src_dir);

    let output_dir = env::var("FIND_UNSAFE2_OUTPUT_DIR").unwrap();
    let output_dir = Path::new(&output_dir);
    assert!(output_dir.is_absolute(),
        "expected $FIND_UNSAFE2_OUTPUT_DIR to be an absolute path, but got {:?}", output_dir);
    fs::create_dir_all(&output_dir).unwrap();

    let args = env::args().collect::<Vec<_>>();
    let r = rustc_public::run_with_tcx!(&args[1..], |tcx| {
        let mut found_src = false;
        let mut files_seen = HashSet::new();
        let items = rustc_public::all_local_items();
        for item in items {
            let file = item.span().get_filename();
            if files_seen.insert(file.clone()) {
                if let Ok(file_abs) = Path::new(&file).canonicalize() {
                    if file_abs.starts_with(&src_dir) {
                        found_src = true;
                        break;
                    }
                }
            }
        }

        // Only process the current crate if it's inside the `SRC_DIR`.
        if found_src {
            let out = find_unsafe2::process(tcx);

            let out_path = output_dir.join(format!("{}.json", rustc_public::local_crate().name));
            serde_json::to_writer(
                File::create(&out_path).unwrap(),
                &out,
            ).unwrap();
        }

        ControlFlow::<(), ()>::Continue(())
    });

    match r {
        Ok(()) => {},
        Err(CompilerError::Failed) => panic!("compilation failed"),
        Err(CompilerError::Interrupted(())) => {},
        Err(CompilerError::Skipped) => {},
    }
}
