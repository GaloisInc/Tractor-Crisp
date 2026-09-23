extern crate proc_macro;
use proc_macro::TokenStream;

/// Emits an unsafe impl from a derive defined inside the project.
#[proc_macro_derive(ProjectSend)]
pub fn project_send(_input: TokenStream) -> TokenStream {
    "unsafe impl Send for Handle {}".parse().unwrap()
}
