# Proposal Task 1 fix round 2 report

## Scope

- Fixed only the remaining Important finding in
  `task-1-rereview-1.md`, on top of
  `4f9a2762b63fcbd1c02ba72b1c395c3d4affda17`.
- Did not change runtime validation, proposal behavior, or the SDD ledger.

## RED evidence

Added a parity test containing one `files[].path` equal to a
`proposal_artifact.path`. It first establishes that a generic Draft 2020-12
validator accepts the shape and PCO runtime rejects it, then interprets the
expected top-level contract extension. Before the schema change:

```text
pytest tests/test_proposal_scaffold.py::test_schema_documents_cross_collection_path_disjointness -q
F
KeyError: 'x-pco-disjoint-by'
```

This demonstrated that the runtime rule existed but was absent from the
packaged contract metadata.

## Fix

- Added top-level `x-pco-disjoint-by` metadata describing the disjoint sets:
  - `files[].path`;
  - `input_material[].path` where `kind == "proposal_artifact"`.
- Added a top-level `$comment` stating that generic JSON Schema validators do
  not enforce the extension.
- The parity test checks the exact declaration and interprets it to reject the
  conflicting manifest, while continuing to prove that the generic validator
  accepts the same shape and runtime validation rejects it.
- No standard keyword was invented and no claim was made that Draft 2020-12
  performs the cross-array join.

## Verification

- Isolated GREEN: **1 passed**.
- Focused and adjacent regression:
  `pytest -o addopts='' tests/test_proposal_scaffold.py tests/test_workspace_layout.py tests/test_workspace_verify.py tests/test_versioning.py -q`
  — **107 passed in 10.41s**.
- Full suite: `pytest -o addopts='' -q` — **275 passed in 54.14s**.
- `python -m build packages/pco --wheel` successfully built exactly
  `pco-0.5.0-py3-none-any.whl`.
- Wheel audit found exactly four proposal source/module entries, verified the
  manifest schema was byte-for-byte identical to source and contained the
  expected disjointness extension, and found zero cache/bytecode entries.
- The generated wheel and empty `dist` directory were removed after audit.
- `git diff --check` and `git diff --cached --check` were silent.

## Remaining concern

Consumers that need full PCO manifest semantics must interpret the documented
`x-pco-disjoint-by` extension or use `ProposalManifest`; generic JSON Schema
validation alone deliberately cannot enforce this cross-collection rule.
