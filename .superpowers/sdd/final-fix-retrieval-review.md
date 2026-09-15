# Final bounded-retrieval fix review

## Verdict

**CLEAN.** No Critical, Important, or Minor findings in the scoped uncommitted change.

Reviewed only:

- `packages/pco/src/pco/retrieval.py`
- `tests/conftest.py`
- `tests/test_retrieval_v05.py`

The review used the final-branch Important #3 finding, the derivation/CLI clean-break plan, and its decision ledger as the governing contract.

## Contract audit

- Search captures one canonical HEAD and validates the exact-commit v0.5 Profile before choosing a path.
- A valid generation path verifies the descriptor-bound closed snapshot, canonical manifest, kind, source commit, Profile version, closed file/directory set, and every manifest hash. It consumes `documents.json` and `reverse_refs.json` from that accepted snapshot without calling `canonical_documents`, `_bounded_canonical_documents`, or `_iter_exact_stream_records`.
- Generation payload validation requires canonical JSON, the exact document field set and field types, one of the nine canonical business streams, strictly sorted unique `(stream, id, revision)` keys, contiguous per-ID revisions, exact `previous_revision`, tombstone-aware latest/current flags, and an exact recomputation of reverse references.
- Missing, stale, malformed, hash-invalid, internally inconsistent, backend-failing, or post-read-mutated generations enter canonical fallback.
- Fallback streams Git blobs from the captured commit twice. Per-ID latest revision/status state is kept in an external SQLite `WITHOUT ROWID` table; Python retains only a fixed-size top-K heap plus one streamed record at a time. SQLite uses journal-off, memory temp storage, and a fixed 64 KiB page cache. The second pass implements the same current, historical, change, pattern, and continuity ordering/filtering as the prior full-corpus selector, including continuity's select-latest-before-time-window behavior.
- Only the at-most-K retained records are converted to retrieval documents. The regression observes `_document_from_record` calls, so it checks materialization rather than merely `candidate_count`.
- Fallback and Tantivy scratch state reuse the same fixed-parent, descriptor/identity-bound private-root protocol. Placement ignores `TMPDIR`, rejects a non-real or protected parent, does not follow symlinks/special files, closes SQLite before cleanup, preserves rebound replacements, never reauthorizes a rejected name, and performs bounded cleanup of independently authorized entries. Canonical, workspace, and published generation trees remain unchanged.
- The remaining inability to detect a completely self-consistent corpus replacement accompanied by a correspondingly rewritten manifest is the explicitly accepted no-canonical-digest tradeoff in this fix, not a defect against the approved contract.

## Verification

- `pytest tests/test_retrieval_v05.py -q` — passed to 100%.
- `PCO_RUN_MILVUS=1 pytest tests/test_retrieval_v05.py tests/test_real_index_backends.py tests/test_derive_all.py tests/test_v05_cli.py tests/test_v05_cli_workspace.py -q` — passed to 100%.
- `black --check packages/pco/src/pco/retrieval.py tests/conftest.py tests/test_retrieval_v05.py` — all three files unchanged.
- `git diff --check -- packages/pco/src/pco/retrieval.py tests/conftest.py tests/test_retrieval_v05.py` — clean.
- Fixed `/tmp` residue check after the test runs — no `pco-query-*` entry remained.

One discarded test invocation named nonexistent `tests/test_cli.py`; it failed immediately during collection and was replaced by the two actual v0.5 CLI test modules listed above. It provides no product signal.
