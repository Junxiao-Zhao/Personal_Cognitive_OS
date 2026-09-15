# Final source-runtime and proposal-error fix review

Review target: current unstaged fixes for final-branch-review Important #1 and #2
Review mode: independent, read-only (except this ignored report)
Verdict: **CLEAN**

## Findings

### Critical

None.

### Important

None.

### Minor

None.

## Important #1: v0.5 source clean break

- `packages/pco/src/pco/sources.py` now ends with the v0.5 read-only `source_diff` implementation. The obsolete `SourceRead`, `SourceReader`, `normalize_text`, `content_hash`, `_validate_locator`, `read_local_file`, `ReaderRegistry`, `SourceManager`, and `SourceReaderRegistry` runtime, including direct registration, materialization, and canonical-write paths, is removed together with its unused imports.
- Repository-wide shipped-code/test searches found no remaining consumer or definition of `SourceManager`, `ReaderRegistry`, `register_local`, `register_locator`, `register_remote`, or `collect_diffs`; only explicit negative clean-break assertions mention the removed names.
- `tests/test_source_diff_v05.py` no longer imports or monkeypatches `SourceManager`, while preserving the material-read trap for remote source diff.
- `tests/test_clean_break.py` proves the nine removed symbols are absent both from the source import and from a freshly built wheel. It also proves `source add`, `source register`, and `source materialize` are rejected and the installed capability map remains the exact v0.5 allowlist.
- This satisfies the final-removal obligation recorded by proposal ruling P80 and Derivation/CLI/Clean-break Task 5 / ruling D111.

## Important #2: exact proposal error envelope

- `ProposalService.validate` now returns `MemError.as_dict()` unchanged. It no longer merges diagnostics into `error`.
- The real validation failure test asserts that `error` has exactly the six public fields: `code`, `phase`, `message`, `canonical_changed`, `retry_safe`, and `recovery`; the observed hash remains under sibling `diagnostic.value`.
- The zero-active and wrong-requested-ID service tests retain the required `PROPOSAL_NOT_FOUND` and `PROPOSAL_MANIFEST_MISMATCH` distinction and assert their path diagnostics remain at sibling `diagnostic.path`.
- The CLI delegates proposal validation directly to the service, so no downstream reshaping reintroduces the incompatible envelope.

## Verification evidence

- `pytest -p no:cacheprovider -q tests/test_source_diff_v05.py tests/test_clean_break.py tests/test_proposal_validation.py tests/test_error_contract.py tests/test_v05_cli.py tests/test_v05_cli_workspace.py` — **passed (123 tests)**.
- `git diff --check` — clean.
- Fresh-wheel absence probe ran through `tests/test_clean_break.py` and passed.
- Worktree status before and after verification contained only the coordinator's existing eight unstaged implementation/test edits; this review created no tracked changes.

The concurrent retrieval fix files were intentionally excluded from this review.
