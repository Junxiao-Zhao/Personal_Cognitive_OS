# Task 6 Report: Canonical Append-Only, Error, and Package Boundaries

## Status

Complete. This report is included in the Task 6 commit. Because a commit hash cannot be embedded in the commit that defines it without changing the hash, the final hash is reported in the handoff response.

## Inherited Audit

- Starting point: `b819f4aeb37f7e8eb77e09f5749d05f10a1a4c5e`.
- Inherited state had a broad uncommitted partial migration across mem-core and PCO, including new `tests/test_error_contract.py` and `tests/test_v05_append_only.py`.
- Before my edits, the inherited focused Task 6 tests passed:
  - `pytest tests/test_v05_append_only.py tests/test_error_contract.py tests/test_registry_discovery.py -v`: 17 passed.
  - `pytest tests/test_v05_profile.py tests/test_v05_validation.py tests/test_v05_append_only.py tests/test_error_contract.py tests/test_application_commit.py tests/test_mem_core_locking.py tests/test_registry_discovery.py -v`: 77 passed.
  - `pytest -q`: exit 0.
- I did not count those inherited passing tests as RED evidence.
- Source audit found no remaining `retryable` string after the inherited migration, but many public `MemError(...)` and `ensure(...)` call sites still relied on default `canonical_changed=False, retry_safe=False`.

## Migration Inventory

- Replaced the public error shape field `retryable` with `canonical_changed` and `retry_safe`.
- Tightened `MemError` and `ensure` so callers must pass both canonical error fields explicitly.
- Repaired all mem-core and PCO public structured error call sites, including legacy runtime modules that remain live until the later deletion plan.
- Preserved old safe-retry semantics:
  - old `retryable=True` paths now use `canonical_changed=False, retry_safe=True`.
  - post-fast-forward cleanup failure uses `canonical_changed=True, retry_safe=True`.
  - stale-head and integrity failures use `canonical_changed=False, retry_safe=False`.
- Added explicit exact recovery commands where the task had a concrete command or existing recovery guidance, including `pco proposal commit <review_id>` for cleanup retry.
- Mechanical audit after migration:
  - `rg -n "retryable" packages tests`: no matches.
  - AST audit of `MemError(...)` and `ensure(...)`: all calls explicit.

## Hook, Registry, and Version Work

- Hook enforcement now checks byte-level append-only behavior for every v0.5 Profile JSONL stream plus `transactions/transactions.jsonl`.
- Hook now rejects changed or deleted `workspace.json` bytes after the root commit.
- Package versions are exactly `0.5.0` in:
  - `packages/mem-core/pyproject.toml`
  - `packages/mem-core/src/mem_core/__init__.py`
  - `packages/pco/pyproject.toml`
  - `packages/pco/src/pco/__init__.py`
- PCO now depends on `mem-core>=0.5.0,<0.6.0`.
- `mem_core.capabilities` retains only:
  - `pco.validate`
  - `pco.retrieval.search`
  - `pco.index.build`
  - `pco.projection.markdown`
- Removed registry exposure for AFFiNE projection, backlinks, context, and research capabilities without deleting their legacy runtime modules.

## TDD Evidence

- Inherited tests already passed, so they are recorded as characterization only.
- New RED test:
  - Added `test_structured_error_requires_canonical_retry_contract`.
  - `pytest tests/test_error_contract.py -v`: 1 failed, 1 passed.
  - Expected failure: `DID NOT RAISE <class 'TypeError'>` because `MemError(...)` still allowed omitted canonical fields.
- GREEN:
  - Made `MemError` and `ensure` require `canonical_changed` and `retry_safe`.
  - Repaired omitted call sites.
  - `pytest tests/test_error_contract.py -v`: 2 passed.

## Final Verification

- `pytest tests/test_v05_append_only.py tests/test_error_contract.py tests/test_registry_discovery.py -v`: 18 passed.
- `pytest tests/test_v05_profile.py tests/test_v05_validation.py tests/test_v05_append_only.py tests/test_error_contract.py tests/test_application_commit.py tests/test_mem_core_locking.py tests/test_registry_discovery.py -v`: 78 passed.
- `pytest -q`: exit 0.
- `pytest --collect-only -q`: 90 tests collected across 10 files.
- `git diff --check`: exit 0, no output.

## Files

- mem-core package metadata, public exports, errors, hook, lock/repository/application/transaction/profile/delta/CLI paths.
- PCO package metadata, public exports, CLI, workspace/config, sources, retrieval, projections, validation, checkpoint runtime modules, and harness runtime modules.
- Tests: `tests/test_v05_append_only.py`, `tests/test_error_contract.py`, `tests/test_registry_discovery.py`, plus focused assertions in application, locking, profile validation, and v0.5 validation tests.

## Self-Review

- Reviewed the full diff from `b819f4aeb37f7e8eb77e09f5749d05f10a1a4c5e`.
- Highest-risk semantics checked:
  - append-only checks run before transaction validation so historical tampering reports `APPEND_ONLY_VIOLATION`.
  - `workspace.json` is compared against root commit bytes, not merely the previous commit.
  - application cleanup happens after canonical fast-forward, so its error is changed=true/safe=true.
  - stale-head and integrity failures remain changed=false/safe=false.
  - registry cleanup removes v0.4 exposure only, not the whole legacy runtime.
- `requesting-code-review` would normally dispatch a reviewer, but this task explicitly said not to dispatch subagents, so I performed the required self-review directly.

## Concerns

- The stricter `MemError`/`ensure` API is intentionally breaking for old in-repo callers that omit canonical fields. All in-repo call sites have been repaired and verified; external callers using the old constructor shape must migrate.

## Fix Round 1 Addendum

### Inherited and Review Audit

- Starting fix round from committed Task 6 baseline: `f9f48c00cf821070d8246363cd3cafcb9c12ab7c`.
- Re-audited the combined Task 6 diff from `b819f4aeb37f7e8eb77e09f5749d05f10a1a4c5e` before committing this fix round:
  - `git diff --stat b819f4aeb37f7e8eb77e09f5749d05f10a1a4c5e -- packages tests .superpowers/sdd/2026-08-30-pco-v0.5-canonical-core/task-6-report.md`: 40 files changed, 4589 insertions, 902 deletions.
  - `git diff --name-only b819f4aeb37f7e8eb77e09f5749d05f10a1a4c5e -- packages tests`: expected Task 6 mem-core, PCO, and test surface only.
- Confirmed the review findings were valid:
  - pre-commit policy loaded `.mem-profile.json` and Profile content from the unstaged working tree.
  - hook `write_artifact` receipt validation did not call `profile.artifact_path(...)`.
  - `ErrorDetail.as_dict()` leaked diagnostics inside public `error`.
  - AFFiNE projection returned retained public error mappings with missing `phase`, `message`, or `recovery`, and placed `diagnostic_path` inside `error`.
- Workspace root `workspace.json` initialization remains intentionally out of scope for this fix round per the reviewer note; hook enforcement remains ready to enforce it when present.

### Fix Round TDD Evidence

- RED: added regression tests before production changes, then ran `pytest tests/test_v05_append_only.py tests/test_error_contract.py -v`.
  - Result: 20 collected, 5 failed, 15 passed.
  - Expected failures:
    - same-length historical JSONL rewrite with unstaged Profile omission committed successfully instead of failing with `APPEND_ONLY_VIOLATION`.
    - crafted `write_artifact` outside allowed artifact roots committed successfully instead of failing with `ARTIFACT_PATH_NOT_ALLOWED`.
    - public `MemError.as_dict()["error"]` included diagnostic keys (`stream`, `record_id`, `path`, `value`).
    - AFFiNE no-bridge result omitted required public error fields.
    - AFFiNE failed-bridge result kept `diagnostic_path` inside `error` and omitted required public error fields.
- GREEN after implementation:
  - `pytest tests/test_v05_append_only.py tests/test_error_contract.py -v`: 20 passed.
- During full verification, Python 3.13 emitted a tar extraction deprecation warning from the new trusted-tree Profile archive loader.
  - Added a compatibility-safe `extractall(..., filter="data")` path with fallback for older Python.
  - `pytest -W error::DeprecationWarning tests/test_profile_validation_modes.py::test_logs_stream_uses_profile_fast_path_for_transaction_and_hook -q`: 1 passed.

### Fix Round Migration Inventory

- Hook policy now resolves `.mem-profile.json` and Profile bytes from trusted Git trees:
  - non-root commits use `HEAD`, so unstaged marker/Profile edits cannot omit protected streams.
  - root initialization uses the staged tree produced by `git write-tree`, then returns root-initialization success without weakening later commits.
  - marker/Profile paths are constrained to relative non-escaping paths before archive extraction.
  - archive extraction rejects symlinks, hardlinks, devices, and escaping paths; safe tar filtering is requested when supported.
- Hook `write_artifact` receipt authorization now validates every operation path through `Profile.artifact_path(...)`, enforcing Profile artifact roots.
- Public `MemError.as_dict()["error"]` is exactly six fields: `code`, `phase`, `message`, `canonical_changed`, `retry_safe`, `recovery`.
- Optional `stream`, `record_id`, `path`, and `value` diagnostics now appear only in top-level `diagnostic`.
- PCO checkpoint structured errors and retained AFFiNE projection error results now expose exact six-field `error` mappings; AFFiNE bridge diagnostics are top-level `diagnostic`.

### Fix Round Verification

- `pytest tests/test_v05_append_only.py tests/test_error_contract.py tests/test_registry_discovery.py -v`: 23 passed.
- `pytest tests/test_v05_profile.py tests/test_v05_validation.py tests/test_v05_append_only.py tests/test_error_contract.py tests/test_application_commit.py tests/test_mem_core_locking.py tests/test_registry_discovery.py -v`: 83 passed.
- `pytest -q`: exit 0, no warning summary.
- `git diff --check`: exit 0, no output.
- `rg -n "retryable|['\"]error['\"]\s*:\s*\{" packages tests`: no matches.

### Fix Round Files

- `packages/mem-core/src/mem_core/errors.py`
- `packages/mem-core/src/mem_core/hook.py`
- `packages/pco/src/pco/checkpoint/errors.py`
- `packages/pco/src/pco/projections.py`
- `tests/test_error_contract.py`
- `tests/test_v05_append_only.py`
- `.superpowers/sdd/2026-08-30-pco-v0.5-canonical-core/task-6-report.md`

### Fix Round Commit and Concerns

- Fix commit: final hash reported in the handoff response because embedding the hash in the committed report would change the hash.
- Concern: external consumers that expected diagnostics inside `error` must read top-level `diagnostic` instead.
- Concern: root `workspace.json` bootstrap is still owned by Workspace-and-Skills Task 5, not Task 6.

## Fix Round 2 Addendum

### Inherited and Review Audit

- Starting fix round from committed Task 6 fix baseline: `3fb1eb33ee7b9d46715eda88543ca14d115ca1b1`.
- Confirmed `MemoryRepository.init(copy_profile=False)` writes the resolved absolute external Profile path to committed `.mem-profile.json`.
- Confirmed the hook trusted-tree loader rejected every absolute marker path through `_trusted_relative_path(...)`, so init succeeded but the first verified transaction commit failed with `PROFILE_MARKER_INVALID`.
- Confirmed the secure root cause was not just "allow absolute paths": without a committed external Profile identity, the hook would have to trust mutable Profile bytes outside Git and could reintroduce the protected-stream/artifact-root bypass fixed in round 1.

### Fix Round 2 TDD Evidence

- RED: added `test_copy_profile_false_external_profile_commits_public_transaction` and `test_external_profile_mutation_after_init_fails_closed_before_policy_can_weaken` before production changes, then ran `pytest tests/test_mem_core.py::test_copy_profile_false_external_profile_commits_public_transaction tests/test_mem_core.py::test_external_profile_mutation_after_init_fails_closed_before_policy_can_weaken -v`.
  - Result after fixing one test import typo and rerunning RED: 2 collected, 2 failed.
  - Expected failures:
    - public external-Profile commit raised `MemError` from Git commit hook with `PROFILE_MARKER_INVALID` and message `Profile paths must be relative`.
    - weakened external-Profile test got `PROFILE_MARKER_INVALID` instead of the required committed-identity failure `PROFILE_TREE_MISMATCH`.
- GREEN after implementation:
  - `pytest tests/test_mem_core.py::test_copy_profile_false_external_profile_commits_public_transaction tests/test_mem_core.py::test_external_profile_mutation_after_init_fails_closed_before_policy_can_weaken -v`: 2 passed in 2.62s.
- Post-GREEN readability cleanup rerun:
  - `pytest tests/test_mem_core.py tests/test_v05_append_only.py tests/test_profile_validation_modes.py -v`: 28 passed in 28.95s.

### Fix Round 2 Implementation

- Added `mem_core.profile_identity` to compute a deterministic SHA-256 digest over sorted regular Profile files and to snapshot verified external Profile bytes into hook-local temporary storage.
- External Profile identity is committed only for `copy_profile=False` markers:
  - `profile_tree_digest`: exact regular-file tree content identity.
  - `policy_hash`: parsed Profile policy identity.
- Hook loading behavior:
  - relative copied Profile markers still load from the trusted Git tree exactly as before.
  - absolute external Profile markers require `profile_tree_digest` and `policy_hash`.
  - the hook snapshots only regular files from the external Profile, rejects symlinks and special entries, compares the snapshot digest with the committed marker, loads the Profile from the snapshot, and verifies the parsed policy hash.
  - missing, mutated, or unsafe external Profiles fail closed before transaction policy can be weakened.
- `copy_profile=False` remains external; the full Profile is not copied into the canonical repository.

### Fix Round 2 Verification

- Covering external-profile/append-only/profile-mode gate:
  - `pytest tests/test_mem_core.py tests/test_v05_append_only.py tests/test_profile_validation_modes.py -v`: 28 passed in 28.95s.
- Task 6 focused gate:
  - `pytest tests/test_v05_append_only.py tests/test_error_contract.py tests/test_registry_discovery.py -v`: 23 passed in 21.33s.
- Canonical-core gate:
  - `pytest tests/test_v05_profile.py tests/test_v05_validation.py tests/test_v05_append_only.py tests/test_error_contract.py tests/test_application_commit.py tests/test_mem_core_locking.py tests/test_registry_discovery.py -v`: 83 passed in 26.89s.
- Full suite:
  - `pytest -q`: exit 0. Because `pyproject.toml` already adds `-q`, this ran double-quiet and emitted progress only:
    - `......................................................`
    - `.................. [ 74%]`
    - `.........................                                                [100%]`
- `git diff --check`: exit 0, no output.

### Fix Round 2 Files

- `packages/mem-core/src/mem_core/profile_identity.py`
- `packages/mem-core/src/mem_core/repository.py`
- `packages/mem-core/src/mem_core/hook.py`
- `tests/test_mem_core.py`
- `.superpowers/sdd/2026-08-30-pco-v0.5-canonical-core/task-6-report.md`

### Fix Round 2 Commit and Concerns

- Fix commit: final hash reported in the handoff response because embedding the hash in the committed report would change the hash.
- Self-review: the fix preserves copied-relative Profile behavior, keeps non-root hook policy anchored to committed `HEAD`, keeps root/no-HEAD validation on the staged tree, and limits the absolute-path exception to external Profiles with committed identity fields.
- Concern: existing external-Profile repositories initialized by the vulnerable marker shape without `profile_tree_digest` and `policy_hash` will fail closed until re-initialized or migrated with a trusted identity marker.
