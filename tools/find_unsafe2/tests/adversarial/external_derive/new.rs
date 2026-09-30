#![allow(unused)]

/// The derive is defined outside the project, so its unsafe impl isn't charged.
#[derive(adv_external_derive_macro::ProjectSend)]
struct Handle {
    p: *const i32,
}
