# Final Important #3 — bounded retrieval fix report

## Scope

Changed only:

- `packages/pco/src/pco/retrieval.py`
- `tests/conftest.py`
- `tests/test_retrieval_v05.py`

No files were staged or committed by this fix agent.

## Implemented behavior

- `search()` captures canonical HEAD once and validates only the exact-commit v0.5 Profile before choosing a retrieval path.
- A valid generation is verified from its closed manifest, hashes, kind, source commit, and Profile version, then consumed from its accepted snapshot. The normal path does not call `canonical_documents()`, `_bounded_canonical_documents()`, or `_iter_exact_stream_records()`.
- Generation payload validation now requires canonical JSON, exact document fields and types, the nine v0.5 business streams, strict sort/uniqueness, contiguous revisions, `previous_revision == null` for revision 1 and `revision - 1` thereafter, recomputed effective-current/tombstone flags, and an exactly recomputed reverse-reference payload.
- Canonical fallback streams strict records from exact Git blobs twice. Pass 1 stores only per-ID latest revision/status in a repo-external SQLite database; pass 2 retains only deterministic top-K records in a Python heap and materializes only that bounded set.
- Fallback mode selection is equivalent to `_select_mode(...)` followed by `_bounded_recent(...)` for `current`, `historical`, `change`, `pattern`, and `continuity`, including tombstones and time windows. Continuity deliberately chooses the latest effective handoff before applying the time window, matching the existing contract.
- SQLite uses a fixed, independent `/tmp` parent and the same descriptor/identity-bound private-root creation and cleanup protocol as the Tantivy query copy. It ignores `TMPDIR`, creates no workspace/generation sidecar, uses `journal_mode=OFF`, closes before cleanup, and leaves no `pco-query-*` root after successful tested paths.
- The backend query-root failure seam remains independently injectable through `_SYSTEM_TEMP_PARENT`; poisoning it correctly triggers canonical fallback without poisoning the fixed SQLite fallback boundary.

## Resource boundary and cost

- Fallback Python retention is `O(K)` where `K = CANONICAL_FALLBACK_CANDIDATES`; per-ID state is disk-backed under the private SQLite root. SQLite's page cache is fixed to 64 KiB.
- Disk usage is `O(distinct IDs)`. Fallback performs two exact-commit raw scans and indexed SQLite operations, so its time cost is approximately `O(N log IDs)` plus ranking over at most K documents.
- Reverse-reference state on fallback is built only for the bounded candidate working set. Pattern graph boosts therefore do not include source documents outside that set.
- A valid generation still parses its own snapshotted document/reverse-reference payloads, but it no longer reads or materializes canonical business documents.
- The valid-generation path no longer byte-compares documents against a freshly rebuilt full canonical payload. It relies on the closed manifest/source-commit binding plus strict snapshot-internal invariants. If canonical byte equivalence must be proven without an `O(N)` rebuild on every search, the generation format needs a canonical-anchored payload digest/proof.

## Regression evidence

- RED: `test_bounded_continuity_selects_latest_before_applying_time_window` failed because the streaming selector returned the older in-window handoff while the established selector returned none.
- RED: `test_private_query_copy_ignores_workspace_tmpdir_and_unsafe_parent_falls_back` failed with a second `MemError` because the SQLite fallback reused the poisoned backend temporary-parent seam.
- GREEN: the two defects above plus both revision-1/revision-N `previous_revision` cases passed as 4 focused cases.
- Valid-generation instrumentation fails if any canonical document builder/streamer is invoked.
- Missing and corrupt generation tests observe that exactly K documents are materialized, and preserve both canonical workspace bytes and generation bytes/layout.
- Five-mode equivalence tests compare the bounded streaming selector directly with `_select_mode(...)` + `_bounded_recent(...)`; dedicated coverage includes tombstones and continuity's post-selection time window.

## Verification evidence

- `pytest tests/test_retrieval_v05.py -q` — exit 0, 68 tests passed.
- `pytest tests/test_retrieval_v05.py -k 'private_query or cleanup or fallback_materializes or bounded_streaming or bounded_continuity or verified_generation_never' -q` — exit 0, 17 cleanup/bounded tests passed.
- `PCO_RUN_MILVUS=1 pytest tests/test_real_index_backends.py -q` — exit 0, 4 real-backend tests passed.
- `pytest tests/test_current_projection.py tests/test_derive_all.py tests/test_v05_cli.py tests/test_v05_cli_workspace.py tests/test_v05_acceptance.py -q` — exit 0, 108 related projection/derive/CLI/acceptance tests passed.
- `black --workers 1 --check packages/pco/src/pco/retrieval.py tests/conftest.py tests/test_retrieval_v05.py` — exit 0; all three files unchanged.
- `python -m py_compile ...` with an explicit `/tmp/pco-pycache-bounded-retrieval-*` cache prefix — exit 0; the exact temporary cache was removed afterward.
- `git diff --check` — exit 0.
- `find /tmp -maxdepth 1 -name 'pco-query-*'` — no output after tests.
