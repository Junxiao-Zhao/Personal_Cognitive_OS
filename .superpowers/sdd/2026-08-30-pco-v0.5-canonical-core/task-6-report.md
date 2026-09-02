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
