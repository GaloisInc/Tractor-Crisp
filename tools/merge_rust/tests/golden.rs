use std::collections::{HashMap, HashSet};
use std::env;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;


fn read_tree(path: impl AsRef<Path>) -> io::Result<HashMap<PathBuf, String>> {
    fn go(path: &Path, sub_path: &Path, out: &mut HashMap<PathBuf, String>) -> io::Result<()> {
        let full_path = path.join(sub_path);
        for entry in fs::read_dir(&full_path)? {
            let entry = entry?;
            let name = entry.file_name();
            let child_sub_path = sub_path.join(&name);
            if entry.file_type()?.is_dir() {
                go(path, &child_sub_path, out)?;
            } else {
                let name_str = name.into_string().unwrap();
                if name_str.starts_with(".") {
                    continue;
                }
                let s = fs::read_to_string(&path.join(&child_sub_path))?;
                out.insert(child_sub_path, s);
            }
        }
        Ok(())
    }
    let mut out = HashMap::new();
    go(path.as_ref(), Path::new(""), &mut out)?;
    Ok(out)
}

fn write_tree(path: impl AsRef<Path>, m: &HashMap<PathBuf, String>) -> io::Result<()> {
    let path = path.as_ref();
    for (sub_path, s) in m {
        let full_path = path.join(&sub_path);
        let parent = full_path.parent().unwrap();
        fs::create_dir_all(parent)?;
        fs::write(full_path, s)?;
    }
    Ok(())
}

fn golden_dir() -> PathBuf {
    let tests_dir = Path::new(file!()).parent().unwrap();
    tests_dir.join("golden")
}

fn test(file_name: &str) -> io::Result<()> {
    let golden_dir = golden_dir();
    let test_dir = golden_dir.join(file_name);
    let input_dir = test_dir.join("input");
    let output_dir = test_dir.join("output");
    let good_dir = test_dir.join("good");

    if fs::exists(&output_dir)? {
        fs::remove_dir_all(&output_dir)?;
    }

    eprintln!("read {input_dir:?}");
    let input = read_tree(&input_dir)?;
    eprintln!("write {output_dir:?}");
    write_tree(&output_dir, &input)?;
    drop(input);

    let status = Command::new(env::var_os("CARGO").unwrap())
        .arg("run")
        .arg("--manifest-path").arg(env::var_os("CARGO_MANIFEST_PATH").unwrap())
        .arg("--")
        .arg(output_dir.join("lib.rs"))
        .arg(test_dir.join("snippets.json"))
        .status()?;
    assert!(status.success(), "subcommand failed");

    eprintln!("read {output_dir:?}");
    let output = read_tree(&output_dir)?;
    if !fs::exists(&good_dir)? {
        // On first run, populate `good/`
        write_tree(&good_dir, &output)?;
    } else {
        eprintln!("read {good_dir:?}");
        let good = read_tree(&good_dir)?;
        if output != good {
            let mut all_keys = output.keys().chain(good.keys()).collect::<Vec<_>>();
            all_keys.sort();
            all_keys.dedup();
            let mut error_count = 0;
            for key in all_keys {
                let output_s = output.get(key);
                let good_s = good.get(key);
                if output_s == good_s {
                    continue;
                }
                if let Some(output_s) = output_s {
                    eprintln!(" === output {key:?} ===\n{output_s}");
                } else {
                    eprintln!(" === output {key:?}: not found ===");
                }
                if let Some(good_s) = good_s {
                    eprintln!(" === good {key:?} ===\n{good_s}");
                } else {
                    eprintln!(" === good {key:?}: not found ===");
                }
                eprintln!(" =======");
                error_count += 1;
            }
            panic!("found {} mismatches in output", error_count);
        }
    }

    Ok(())
}

fn check_for_missing_tests_helper(known_tests: HashSet<&'static str>) -> io::Result<()> {
    let mut missing_tests = Vec::new();
    let golden_dir = golden_dir();
    for entry in fs::read_dir(&golden_dir)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        let name = entry.file_name().into_string().unwrap();
        if known_tests.contains(&name as &str) {
            continue;
        }
        if !fs::exists(golden_dir.join(&name).join("snippets.json"))? {
            continue;
        }
        missing_tests.push(name);
    }
    if missing_tests.len() > 0 {
        panic!("tests exist on disk, but aren't listed in tests/golden.rs: {:?}", missing_tests);
    }
    Ok(())
}


macro_rules! define_tests {
    ($($name:ident,)*) => {
        $(
            #[test]
            fn $name() -> io::Result<()> {
                test(stringify!($name))
            }
        )*

        #[test]
        fn check_for_missing_tests() -> io::Result<()> {
            let known_tests = HashSet::from([ $( stringify!($name), )* ]);
            check_for_missing_tests_helper(known_tests)
        }
    }
}

define_tests! {
    update,
    add_remove,
    add_module,
}
