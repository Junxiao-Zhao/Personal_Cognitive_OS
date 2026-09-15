# PCO v0.5 final branch re-review

Review range: `8d28856..a251c39`
Reviewed head: `a251c3904a1211414b027399670b1293fdd6f8b6`
Verdict: **CLEAN**
Ready: **Yes**, subject to the explicitly separate manual Host TUI release gate below.

This was a fresh, read-only integrated review of all 26 tasks in the four approved plans. I read the re-review package, the prior whole-branch review, the approved design and clean-break ADR, all four plan/progress ledgers, and the final-fix implementation/re-review reports; I then inspected the branch and ran independent focused verification rather than relying on the coordinator's reported full-suite results.

## Findings

### Critical

None.

### Important

None.

### Minor

None.

## Closure of the three prior Important findings

1. **Legacy direct source runtime — closed.** `packages/pco/src/pco/sources.py:1-260` now contains only the v0.5 read-only source-diff implementation; the nine legacy reader/registration/materialization symbols and their write paths are absent. `tests/test_clean_break.py:105-180` checks source and wheel symbol absence, exact capability inventory, and rejected source write commands. Repository-wide shipped-code and consumer searches found no surviving definition or use.

2. **Proposal six-field error contract — closed.** `packages/pco/src/pco/proposals/service.py:62-90` returns `MemError.as_dict()` unchanged. `tests/test_proposal_validation.py:105-118` freezes exactly `code`, `phase`, `message`, `canonical_changed`, `retry_safe`, and `recovery` under `error`, while actionable values and paths remain under sibling `diagnostic`; zero-active and wrong-ID behavior remain distinct.

3. **Bounded canonical retrieval fallback — closed.** `packages/pco/src/pco/retrieval.py:1340-1422` captures one HEAD, accepts a generation only after closed manifest/hash/control and payload validation, and does not rebuild canonical documents on the valid-generation path. `packages/pco/src/pco/retrieval.py:1099-1337` performs two exact-commit streaming passes; per-ID latest state is disk-backed in descriptor-bound fixed-`/tmp` SQLite with a 64 KiB cache, while Python retains and materializes at most K records. Cleanup and backend scratch placement use identity-bound private roots and the backend failure seam is independent from fallback state placement.

## Task and ruling audit

- The commit sequence and resulting tree cover all 26 planned tasks: canonical core 6/6, workspace and SKILLs 7/7, proposal and sources 7/7, derivation/CLI/clean break 6/6.
- All 55 pre-existing ledger rulings are acceptable at the reviewed head. The prior P80/D111 source-removal rejections are closed by `7194297`; the prior D75 bounded-fallback question is closed by `76032d6`.
- All five final-integration rulings are accepted, for a final audit of **60/60 accepted**. Their costs are stated accurately: valid-generation trust is self-consistency plus source-commit binding rather than a canonical-anchored payload proof; fallback uses O(distinct IDs) external disk and approximately O(N log IDs) work; fallback pattern boosts see reverse references only within the bounded retained set; and backend-query scratch failure remains isolated from canonical fallback scratch. These are explicit architectural tradeoffs, not hidden violations of the approved v0.5 contract.
- Spot checks across canonical application commits/locking, exact Profile validation, workspace initialization and verification, SKILL manifests, proposal commit/archive, projections, CLI, clean-wheel contracts, package entry points, and removed resources found no integration regression.

## Independent verification evidence

- `pytest -p no:cacheprovider -q tests/test_source_diff_v05.py tests/test_clean_break.py tests/test_proposal_validation.py tests/test_error_contract.py tests/test_retrieval_v05.py tests/test_v05_acceptance.py tests/test_v05_cli.py tests/test_derive_all.py` — exit 0.
- `pytest -p no:cacheprovider -q tests/test_application_commit.py tests/test_mem_core_locking.py tests/test_v05_validation.py tests/test_workspace_init.py tests/test_workspace_verify.py tests/test_skill_resources.py tests/test_proposal_commit.py tests/test_proposal_archive.py tests/test_current_projection.py tests/test_clean_wheel_smoke.py` — exit 0.
- `PCO_RUN_MILVUS=1 pytest -p no:cacheprovider -q tests/test_real_index_backends.py tests/test_retrieval_v05.py -k 'real or verified_generation_never or fallback_materializes or bounded_streaming or bounded_continuity or private_query_copy_ignores'` — exit 0 in an environment with writable owned temporary storage.
- One initial real-backend invocation failed before pytest collection because the read-only sandbox exposed no usable writable temp directory; no product test ran in that attempt. The same selection above passed after using the approved writable-temp execution boundary.
- `python -m compileall -q packages/mem-core/src packages/pco/src` — exit 0.
- `git diff --check 8d28856..a251c39` — no output.
- `git status --short` — no tracked or untracked output.
- `find /tmp -maxdepth 1 -name 'pco-query-*' -print` — no output after verification.
- Package metadata exposes console script `pco` and exactly the retained `pco.validate` and `pco.retrieval.search` capability entry points; removed Harness/checkpoint/OpenCode resource paths are absent.

The coordinator's re-review package additionally records fresh full normal and opt-in suites plus the real isolated clean-wheel smoke at this same head. I treated those as supplementary evidence, not as a substitute for the independent runs above.

## Manual external release gate

**Codex Host TUI smoke: NOT RUN.**
**OpenCode Host TUI smoke: NOT RUN.**

The human protocol remains `docs/verification/pco-v0.5-tui-smoke.md`. It must be executed in both interactive Hosts before release to record project-local SKILL discovery, read-before-verify/fallback behavior, actual prompt and denial/retry observations, resume behavior, review-ID-only commit input, permanent-grant avoidance, and canonical-success/derivation-failure behavior. Automated evidence does not satisfy or imply this external gate.
