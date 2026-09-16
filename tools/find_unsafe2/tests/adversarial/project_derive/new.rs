#![allow(unused)]

/// The derive is defined inside the project, so its unsafe impl is charged.
#[derive(adv_project_derive_macro::ProjectSend)]
struct Handle {
    p: *const i32,
}
