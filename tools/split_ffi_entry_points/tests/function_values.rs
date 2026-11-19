use std::{fs, path::Path, process::Command};

fn read(dir: &Path, file: &str) -> String {
    fs::read_to_string(dir.join(file)).unwrap()
}

fn squash(s: &str) -> String {
    s.split_whitespace().collect()
}

#[test]
fn points_function_values_at_wrappers() {
    let project = tempfile::tempdir().unwrap();
    let dir = project.path();
    // `stored` and `get` take `callback`'s address in separate codegen units of a release build.
    let files = [
        (
            "Cargo.toml",
            "[package]\nname = 'callbacks'\nversion = '0.1.0'\nedition = '2021'\n\
             [lib]\npath = 'lib.rs'\n[profile.release]\ncodegen-units = 16\n",
        ),
        (
            "lib.rs",
            r#"
mod aliased;
mod callbacks;
mod imported;
mod store;
use aliased::aliased as cb;
use imported::imported;
pub type F = unsafe extern "C" fn();
pub fn get_aliased() -> F { cb }
pub fn get_imported() -> F { imported }
#[export_name = "other"] pub unsafe extern "C" fn callback() {}
pub fn call() { unsafe { callback() } }
#[test]
fn same_address() {
    assert_eq!(callbacks::stored() as usize, store::get() as usize);
}
"#,
        ),
        (
            "callbacks.rs",
            "#[no_mangle] pub unsafe extern \"C\" fn callback() {}\n\
             pub fn stored() -> crate::F { static T: crate::F = callback; T }\n",
        ),
        (
            "store.rs",
            "#[inline(never)] pub fn get() -> crate::F { crate::callbacks::callback }\n",
        ),
        (
            "aliased.rs",
            "#[no_mangle] pub unsafe extern \"C\" fn aliased() {}\n",
        ),
        (
            "imported.rs",
            "#[no_mangle] pub unsafe extern \"C\" fn imported() {}\n",
        ),
    ];
    for (name, text) in files {
        fs::write(dir.join(name), text).unwrap();
    }

    let status = Command::new(env!("CARGO_BIN_EXE_split_ffi_entry_points"))
        .arg(dir)
        .status()
        .unwrap();
    assert!(status.success());

    let lib = squash(&read(dir, "lib.rs"));
    assert!(lib.contains("fncallback_ffi"), "{lib}");
    // Direct calls still go to the inner function.
    assert!(lib.contains("unsafe{callback()}"), "{lib}");
    let callbacks = squash(&read(dir, "callbacks.rs"));
    assert!(callbacks.contains("=callback_ffi;"), "{callbacks}");
    let store = squash(&read(dir, "store.rs"));
    assert!(
        store.contains("{crate::callbacks::callback_ffi}"),
        "{store}"
    );
    // `_ffi` wouldn't resolve through an alias or import, so these functions stay unsplit.
    assert!(lib.contains("{cb}") && lib.contains("{imported}"), "{lib}");
    for (name, text) in &files[4..] {
        assert_eq!(read(dir, name).trim_end(), text.trim_end());
    }

    let status = Command::new(env!("CARGO"))
        .args(["test", "--release", "--quiet", "--manifest-path"])
        .arg(dir.join("Cargo.toml"))
        .env("CARGO_TARGET_DIR", dir.join("target"))
        .env_remove("RUSTFLAGS")
        .status()
        .unwrap();
    assert!(status.success());
}
