#![feature(rustc_private)]
extern crate rustc_public;

// Required by rustc_public::run! macro
extern crate rustc_driver;
extern crate rustc_interface;
extern crate rustc_middle;

use std::env;
use std::fs::{self, File};
use std::hash::Hash;
use std::path::Path;
use std::process;
use indexmap::IndexMap;
use rustc_public::error::CompilerError;
use serde_json;
use find_unsafe2::{self, Outputs, FunctionOutputs, TypeOutputs};


/// Check whether the unsafe operations recorded in `new` are a subset of those recorded in `old`.
/// Prints an error for each thing in `new` that doesn't appear in `old`, and returns `false` if it
/// found any such things.
fn check_outputs(old: &Outputs, new: &Outputs) -> bool {
    let Outputs { total_unsafe: _, ref fns, ref types } = *new;
    let mut ok = true;

    // We use this default `FunctionOutputs` as the `old_fn` for items that are defined in `new`
    // but not in `old`.  All unsafety and progress metrics are set to zero, so if the agent adds a
    // new unsafe function that wasn't present before, we detect that as a regression.
    let empty_fn = FunctionOutputs {
        is_unsafe_fn: false,
        is_mut_static: false,
        derefs_raw_ptr: 0,
        calls_unsafe: 0,
        uses_static_mut: IndexMap::new(),
        uses_union_field: IndexMap::new(),
        uses_foreign_fn: IndexMap::new(),
        casts_int_to_ptr: 0,
        sig_contains_raw_ptr: 0,
        is_ffi_entry_point: false,
    };
    for (fn_name, new_fn) in fns {
        let old_fn = old.fns.get(fn_name).unwrap_or(&empty_fn);
        ok &= check_function_outputs(fn_name, old_fn, new_fn);
    }

    let empty_type = TypeOutputs {
        field_contains_raw_ptr: IndexMap::new(),
    };
    for (type_name, new_type) in types {
        let old_type = old.types.get(type_name).unwrap_or(&empty_type);
        ok &= check_type_outputs(type_name, old_type, new_type);
    }

    ok
}

fn check_function_outputs(name: &str, old: &FunctionOutputs, new: &FunctionOutputs) -> bool {
    if old.is_ffi_entry_point {
        // Allow increasing unsafe within FFI entry points.
        return true;
    }

    let FunctionOutputs {
        is_unsafe_fn, is_mut_static, derefs_raw_ptr, calls_unsafe,
        ref uses_static_mut, ref uses_union_field, ref uses_foreign_fn,
        casts_int_to_ptr, sig_contains_raw_ptr,
        is_ffi_entry_point,
    } = *new;
    let mut ok = true;

    ok &= check_bad_flag(old.is_unsafe_fn, is_unsafe_fn,
        || format!("{name}: `unsafe` qualifier"));
    ok &= check_bad_flag(old.is_mut_static, is_mut_static,
        || format!("{name}: `mut` qualifier"));

    ok &= check_count(old.derefs_raw_ptr, derefs_raw_ptr,
        || format!("{name}: raw pointer derefs"));
    ok &= check_count(old.calls_unsafe, calls_unsafe,
        || format!("{name}: unsafe function calls"));

    ok &= check_count_map(&old.uses_static_mut, uses_static_mut,
        |k| format!("{name}: uses of static mut {k}"));
    ok &= check_count_map(&old.uses_union_field, uses_union_field,
        |k| format!("{name}: uses of union field {k}"));
    ok &= check_count_map(&old.uses_foreign_fn, uses_foreign_fn,
        |k| format!("{name}: uses of foreign fn {k}"));

    ok &= check_count(old.casts_int_to_ptr, casts_int_to_ptr,
        || format!("{name}: int-to-pointer casts"));
    ok &= check_count(old.sig_contains_raw_ptr, sig_contains_raw_ptr,
        || format!("{name}: raw pointer types in signature"));

    ok &= check_bad_flag(old.is_ffi_entry_point, is_ffi_entry_point,
        || format!("{name}: FFI export flag"));

    ok
}

fn check_type_outputs(name: &str, old: &TypeOutputs, new: &TypeOutputs) -> bool {
    let TypeOutputs {
        ref field_contains_raw_ptr,
    } = *new;
    let mut ok = true;

    ok &= check_count_map(&old.field_contains_raw_ptr, field_contains_raw_ptr,
        |k| format!("{name}: field {k} raw pointer count"));

    ok
}

fn check_count_map<K: Hash + Eq>(
    old: &IndexMap<K, usize>,
    new: &IndexMap<K, usize>,
    mut desc: impl FnMut(&K) -> String,
) -> bool {
    let mut ok = true;
    for (k, &new_count) in new {
        let old_count = old.get(k).copied().unwrap_or(0);
        ok &= check_count(old_count, new_count, || desc(k));
    }
    ok
}

/// Check a numeric "badness" count.  If the number increased, report an error.
fn check_count(old: usize, new: usize, desc: impl FnOnce() -> String) -> bool {
    if new > old {
        println!("{} increased: {old} -> {new}", desc());
        false
    } else {
        true
    }
}

/// Check the state of a "bad" flag.  If it changed from `false` to `true`, report an error.
fn check_bad_flag(old: bool, new: bool, desc: impl FnOnce() -> String) -> bool {
    if !old && new {
        println!("{} changed: false -> true", desc());
        false
    } else {
        true
    }
}


fn main() {
    let json_dir = env::var("FIND_UNSAFE2_JSON_DIR").unwrap();
    let json_dir = Path::new(&json_dir);
    assert!(json_dir.is_absolute(),
        "expected $FIND_UNSAFE2_JSON_DIR to be an absolute path, but got {:?}", json_dir);

    let args = env::args().collect::<Vec<_>>();
    let r = rustc_public::run_with_tcx!(&args[1..], |tcx| {
        let crate_name = rustc_public::local_crate().name;

        let json_path = json_dir.join(format!("{crate_name}.json"));
        if !fs::exists(&json_path).unwrap() {
            return ControlFlow::<(), ()>::Continue(());
        }

        let old_out: Outputs = serde_json::from_reader(
            File::open(&json_path).unwrap(),
        ).unwrap();

        let new_out = find_unsafe2::process(tcx);

        let ok = check_outputs(&old_out, &new_out);
        if !ok {
            process::exit(1);
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
