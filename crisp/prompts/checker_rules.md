Rules enforced against the step's starting baseline:

- In each non-FFI function, every tracked metric must stay at or below its baseline: raw-pointer dereferences, unsafe calls, inline assembly, uses of mutable statics or union fields, foreign-function and FFI-entry-point uses, int-to-pointer casts, and raw-pointer types in signatures (`NonNull` included). An existing safe function must not become unsafe, and an immutable static must not become mutable.
- New functions and crates are checked against an empty baseline. Moving unsafe operations to another function, changing their kind, or adding an unsafe helper can therefore fail even when the crate-wide total falls. Several invocations may prepare a change, but the completed step must satisfy every per-function check.
- Raw-pointer counts in type fields must not increase. New `unsafe impl` blocks are rejected when they increase the count in their module.
- The exported-symbol set must remain unchanged. Existing FFI entry points are exempt from per-function metric checks and excluded from the unsafe-operation total. The separate FFI review still requires unchanged signatures and thin wrappers; leave exported `unsafe` qualifiers as they are.
- A successful checker exit is required. This checker does not downgrade metric regressions to reviewable warnings; a review cannot override a failed check.
