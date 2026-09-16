Review the uncommitted changes to the Rust project in `{cargo_dir_path}` for safety, correctness, observable behavior, API compatibility, and the FFI rules below. Before approving, inspect the complete baseline-to-candidate diff, including declarations, type aliases, ownership helpers, and count-neutral preparation. Follow relevant callers and cleanup paths. Review the combined change, not just checker diagnostics or exported wrapper bodies. Do not edit files.

{reference_instruction}

The candidate passed the mechanical checks applicable to this mode. Their output is context, not a substitute for review:

```text
{checker_diagnostics}
```

## Safety and compatibility rules

{safety_review_rules}

## FFI entry point rules

{ffi_entry_point_rules}

Report actionable defects introduced by the change, with code locations and an explanation of the violated obligation. Do not report style-only concerns or unrelated preexisting defects. If a material safety or compatibility obligation cannot be assessed, identify the missing evidence instead of treating uncertainty as approval.

If you confirm a fundamental defect that invalidates the proposed design, you may stop early and return FAIL. Explain the defect, the violated obligation, and any directly related findings already established; do not spend time exhaustively reviewing code that must be redesigned. Do not stop at a minor issue when the remaining review can identify other actionable defects. Early rejection is never approval: PASS still requires reviewing the complete change and its relevant callers and cleanup paths.

Use the standard review JSON format. Begin its `overall_explanation` field with exactly one of these lines, followed by your explanation:

- `CRISP_REVIEW: PASS` only after completing the review and finding no defects or unresolved material obligations; use an empty `findings` array and `overall_correctness` of `patch is correct`.
- `CRISP_REVIEW: FAIL` when there are defects, missing essential context, or an incomplete review; explain the reason and use `overall_correctness` of `patch is incorrect`.

Include this verdict exactly once. The CLI renders `overall_explanation` before any findings; CRISP requires the explicit passing verdict and rejects missing, ambiguous, or incomplete results.
