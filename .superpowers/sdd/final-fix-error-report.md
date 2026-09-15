# Final review fix report: proposal validation error contract

Reviewed head: `f20faf7`

Scope: final-review Important #2 only.

## Change

- `ProposalService.validate()` now returns `MemError.as_dict()` unchanged.
- Proposal validation tests require the public `error` mapping to contain
  exactly `code`, `phase`, `message`, `canonical_changed`, `retry_safe`, and
  `recovery`.
- Proposal diagnostics are asserted under the sibling `diagnostic` mapping:
  `value` for material hash details and `path` for missing/mismatched review
  IDs.

## TDD evidence

- RED:
  `python -m pytest -p no:cacheprovider -q tests/test_proposal_validation.py::test_validate_reports_observed_hash_and_is_read_only`
  failed because `error` contained the extra key `value`.
- GREEN (focused service cases): 3 passed for the exact error contract,
  wrong-ID `PROPOSAL_MANIFEST_MISMATCH`, and zero-active
  `PROPOSAL_NOT_FOUND`.

## Verification

- `python -m pytest -p no:cacheprovider -q tests/test_proposal_validation.py tests/test_v05_cli.py tests/test_v05_cli_workspace.py tests/test_error_contract.py`
  — 71 passed.
- `python -m black --check packages/pco/src/pco/proposals/service.py` — clean.
- `python -m black --check --line-ranges 107-118 --line-ranges 683-687 --line-ranges 699-703 tests/test_proposal_validation.py`
  — changed regions clean (the pre-existing whole file is not Black-formatted).
- `python -m py_compile packages/pco/src/pco/proposals/service.py tests/test_proposal_validation.py`
  — passed.
- `git diff --check -- packages/pco/src/pco/proposals/service.py tests/test_proposal_validation.py`
  — clean.

No full suite was run, per task scope. No files were staged or committed.
