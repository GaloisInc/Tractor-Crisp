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


/// How a detected increase is reported.  `Error` fails the check; `Warning` is printed for
/// review but tolerated.
#[derive(Clone, Copy)]
enum Severity {
    Error,
    Warning,
}

/// Check `new` against the `old` baseline:
/// - Functions with no unsafety in the baseline (including newly added functions, which get an
///   all-zero baseline) must stay entirely safe.
/// - Functions that already contained unsafety may move, convert, or grow it; such increases are
///   tolerated with a warning, subject to the crate-wide `total_unsafe` not increasing.
/// - The progress metrics (int-to-pointer casts, raw pointer signatures and fields, foreign fn
///   and FFI entry point uses) and the export set remain per-function non-increasing.
///   EXPERIMENTAL: the signature metric binds only at safe endpoints — see
///   `check_function_outputs`.
/// Prints a line per finding and returns `false` if any hard error was found.
fn check_outputs(old: &Outputs, new: &Outputs) -> bool {
    let Outputs { total_unsafe: _, ref fns, ref types, ref unsafe_impls } = *new;
    let mut ok = true;

    // A new `unsafe impl` asserts a soundness property no metric can verify.  Legitimate uses
    // exist (e.g. `Send` on an owned-state registry), so this is a candidate for review-gated
    // relaxation once a reviewer exists; until then it is an error.
    for (k, &new_count) in unsafe_impls {
        let old_count = old.unsafe_impls.get(k).copied().unwrap_or(0);
        if new_count > old_count {
            println!("new `unsafe impl` {k} added");
            ok = false;
        }
    }

    // We use this default `FunctionOutputs` as the `old_fn` for items that are defined in `new`
    // but not in `old`.  All metrics are zero, so anything a new function carries is an increase:
    // the strict metrics reject it outright, and the unsafety metrics surface it for review.
    let empty_fn = FunctionOutputs {
        total_unsafe: 0,
        filename: String::new(),
        is_unsafe_fn: false,
        is_mut_static: false,
        derefs_raw_ptr: 0,
        calls_unsafe: 0,
        inline_asm: 0,
        uses_static_mut: IndexMap::new(),
        uses_union_field: IndexMap::new(),
        uses_foreign_fn: IndexMap::new(),
        uses_ffi_entry_point: IndexMap::new(),
        casts_int_to_ptr: 0,
        sig_contains_raw_ptr: 0,
        is_ffi_entry_point: false,
        ffi_symbol: None,
    };
    // The symbol set is the ABI: every old symbol must survive, no matter which Rust item
    // carries it, and export status gates the unsafety exemption, so a symbol may not move
    // onto an item the baseline already counted.
    let exports = |fns: &IndexMap<String, FunctionOutputs>| -> IndexMap<String, String> {
        fns.iter()
            .filter_map(|(name, f)| f.ffi_symbol.clone().map(|s| (s, name.clone())))
            .collect()
    };
    let old_exports = exports(&old.fns);
    let new_exports = exports(fns);
    for (sym, old_item) in &old_exports {
        if !new_exports.contains_key(sym) {
            println!("{old_item}: exported symbol {sym} removed");
            ok = false;
        }
    }

    for (fn_name, new_fn) in fns {
        match old.fns.get(fn_name) {
            Some(old_fn) => {
                if old_fn.ffi_symbol != new_fn.ffi_symbol {
                    println!("{fn_name}: exported symbol changed: {} -> {}",
                        fmt_symbol(&old_fn.ffi_symbol), fmt_symbol(&new_fn.ffi_symbol));
                    ok = false;
                }
                ok &= check_function_outputs(fn_name, old_fn, new_fn, false);
            },
            None => {
                if let Some(sym) = &new_fn.ffi_symbol {
                    if old_exports.contains_key(sym) {
                        // A baseline symbol carried by a renamed item: the ABI is intact, and
                        // the item inherits the exemption its predecessor had.
                        continue;
                    }
                    println!("{fn_name}: new exported symbol {sym}");
                    ok = false;
                }
                ok &= check_function_outputs(fn_name, &empty_fn, new_fn, true);
            },
        }
    }

    let empty_type = TypeOutputs {
        filename: String::new(),
        field_contains_raw_ptr: IndexMap::new(),
    };
    for (type_name, new_type) in types {
        let old_type = old.types.get(type_name).unwrap_or(&empty_type);
        ok &= check_type_outputs(type_name, old_type, new_type);
    }

    // Unsafety may move between existing unsafe functions, but the crate-wide total (which
    // excludes FFI entry points) must not increase.
    ok &= check_count(old.total_unsafe, new.total_unsafe, Severity::Error,
        || String::from("total unsafe operations"));

    ok
}

fn check_function_outputs(
    name: &str, old: &FunctionOutputs, new: &FunctionOutputs, is_new: bool,
) -> bool {
    if old.is_ffi_entry_point {
        // Allow increasing unsafe within FFI entry points, but say so: ops
        // migrating into the count-exempt zone is the wrapper-stuffing move
        // the FFI review rejects, and the agent sees this output mid-turn.
        if new.total_unsafe > old.total_unsafe {
            println!("warning: {name}: {} unsafe operations now inside \
                count-exempt FFI entry point (baseline {}); entry points \
                must stay thin", new.total_unsafe, old.total_unsafe);
        }
        return true;
    }

    let FunctionOutputs {
        // The per-function total only selects the severity below; the global total is checked in
        // `check_outputs`.
        total_unsafe: _,
        filename: _,
        is_unsafe_fn, is_mut_static, derefs_raw_ptr, calls_unsafe, inline_asm,
        ref uses_static_mut, ref uses_union_field, ref uses_foreign_fn,
        ref uses_ffi_entry_point,
        casts_int_to_ptr, sig_contains_raw_ptr,
        // Checked at the crate level, where the export sets are in view.
        is_ffi_entry_point: _, ffi_symbol: _,
    } = *new;
    let mut ok = true;

    // Unsafety metrics: hard errors on existing functions that were entirely safe; tolerated
    // with a warning on functions that already contained unsafety, and on new functions — so
    // unsafe operations can relocate into named façade/constructor functions — subject to the
    // global total check and reviewer approval.  The strict metrics below still apply to new
    // functions, which keeps classic laundering helpers (raw-pointer parameters on safe fns,
    // or pointer fields) hard-blocked.
    let unsafety = if old.total_unsafe > 0 || is_new {
        Severity::Warning
    } else {
        Severity::Error
    };

    ok &= check_bad_flag(old.is_unsafe_fn, is_unsafe_fn, unsafety,
        || format!("{name}: `unsafe` qualifier"));
    ok &= check_bad_flag(old.is_mut_static, is_mut_static, unsafety,
        || format!("{name}: `mut` qualifier"));

    ok &= check_count(old.derefs_raw_ptr, derefs_raw_ptr, unsafety,
        || format!("{name}: raw pointer derefs"));
    ok &= check_count(old.calls_unsafe, calls_unsafe, unsafety,
        || format!("{name}: unsafe function calls"));
    ok &= check_count(old.inline_asm, inline_asm, unsafety,
        || format!("{name}: inline asm blocks"));

    ok &= check_count_map(&old.uses_static_mut, uses_static_mut, unsafety,
        |k| format!("{name}: uses of static mut {k}"));
    ok &= check_count_map(&old.uses_union_field, uses_union_field, unsafety,
        |k| format!("{name}: uses of union field {k}"));

    // Progress metrics stay per-function strict: they are the laundering surface.
    ok &= check_count_map(&old.uses_foreign_fn, uses_foreign_fn, Severity::Error,
        |k| format!("{name}: uses of foreign fn {k}"));
    ok &= check_count_map(&old.uses_ffi_entry_point, uses_ffi_entry_point, Severity::Error,
        |k| format!("{name}: uses of FFI entry point {k}"));

    ok &= check_count(old.casts_int_to_ptr, casts_int_to_ptr, Severity::Error,
        || format!("{name}: int-to-pointer casts"));

    // EXPERIMENTAL: the signature gate binds only at safe endpoints.  An `unsafe fn` may
    // carry raw-pointer signatures while a conversion is in flight: it is honestly labeled
    // and self-counts, so it cannot be a terminal hiding place.  A fn may be or become safe
    // only with a pointer-free signature, which closes the create-unsafe-then-flip route.
    if !is_unsafe_fn {
        if is_new || old.is_unsafe_fn {
            if sig_contains_raw_ptr > 0 {
                println!("{name}: safe fn carries {sig_contains_raw_ptr} raw pointer types \
                    in signature; new or newly-safe fns must have pointer-free signatures");
                ok = false;
            }
        } else {
            ok &= check_count(old.sig_contains_raw_ptr, sig_contains_raw_ptr, Severity::Error,
                || format!("{name}: raw pointer types in signature"));
        }
    }

    ok
}

fn fmt_symbol(sym: &Option<String>) -> String {
    match sym {
        Some(s) => format!("{s:?}"),
        None => String::from("none"),
    }
}

fn check_type_outputs(name: &str, old: &TypeOutputs, new: &TypeOutputs) -> bool {
    let TypeOutputs {
        filename: _,
        ref field_contains_raw_ptr,
    } = *new;
    let mut ok = true;

    ok &= check_count_map(&old.field_contains_raw_ptr, field_contains_raw_ptr, Severity::Error,
        |k| format!("{name}: field {k} raw pointer count"));

    ok
}

fn check_count_map<K: Hash + Eq>(
    old: &IndexMap<K, usize>,
    new: &IndexMap<K, usize>,
    severity: Severity,
    mut desc: impl FnMut(&K) -> String,
) -> bool {
    let mut ok = true;
    for (k, &new_count) in new {
        let old_count = old.get(k).copied().unwrap_or(0);
        ok &= check_count(old_count, new_count, severity, || desc(k));
    }
    ok
}

/// Check a numeric "badness" count.  If the number increased, report it at `severity`.
fn check_count(old: usize, new: usize, severity: Severity, desc: impl FnOnce() -> String) -> bool {
    if new > old {
        match severity {
            Severity::Error => {
                println!("{} increased: {old} -> {new}", desc());
                false
            },
            Severity::Warning => {
                println!("warning: {} increased: {old} -> {new}", desc());
                true
            },
        }
    } else {
        true
    }
}

/// Check the state of a "bad" flag.  If it changed from `false` to `true`, report it at
/// `severity`.
fn check_bad_flag(old: bool, new: bool, severity: Severity, desc: impl FnOnce() -> String) -> bool {
    if !old && new {
        match severity {
            Severity::Error => {
                println!("{} changed: false -> true", desc());
                false
            },
            Severity::Warning => {
                println!("warning: {} changed: false -> true", desc());
                true
            },
        }
    } else {
        true
    }
}


fn main() {
    let json_dir = env::var("FIND_UNSAFE2_JSON_DIR").unwrap();
    let json_dir = Path::new(&json_dir);
    assert!(json_dir.is_absolute(),
        "expected $FIND_UNSAFE2_JSON_DIR to be an absolute path, but got {:?}", json_dir);

    let src_dir = env::var("FIND_UNSAFE2_SRC_DIR").unwrap();
    let src_dir = Path::new(&src_dir);
    assert!(src_dir.is_absolute(),
        "expected $FIND_UNSAFE2_SRC_DIR to be an absolute path, but got {:?}", src_dir);

    let args = env::args().collect::<Vec<_>>();
    let r = rustc_public::run_with_tcx!(&args[1..], |tcx| {
        let crate_name = rustc_public::local_crate().name;

        if crate_name == "build_script_build" {
            // Build scripts are skipped, matching `find_unsafe2`.
            return ControlFlow::<(), ()>::Continue(());
        }

        let json_path = json_dir.join(format!("{crate_name}.json"));
        let mut old_out = if fs::exists(&json_path).unwrap() {
            serde_json::from_reader(
                File::open(&json_path).unwrap(),
            ).unwrap()
        } else if find_unsafe2::any_local_item_under(tcx, src_dir) {
            // Treat crate missing in baseline as fully safe so
            // putting unsafe code in new crate is detected.
            Outputs::default()
        } else {
            // Skip dependencies
            return ControlFlow::<(), ()>::Continue(());
        };
        // Baselines written before closure attribution still carry `{closure#N}` entries.
        find_unsafe2::fold_closure_entries(&mut old_out);

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
