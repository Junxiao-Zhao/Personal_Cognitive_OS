# PCO MVP verification

This file maps PRD v0.3.1 acceptance criteria to reproducible evidence. It deliberately separates repository conformance from deployment-specific end-to-end checks: a fake AFFiNE bridge proves the projector contract, but it is not evidence that a particular AFFiNE workspace accepted the pages. OpenCode authorization is PASS only when an executable question-lifecycle loopback proves host-issued Yes/No grants; static command text is not evidence.

## Automated acceptance matrix

| AC | Result | Reproducible evidence |
|---|---|---|
| AC-01 source cold start | PASS | `test_ac01_source_cold_start_commits_four_classes_meta_and_continuation` registers a read-only journal without bootstrap, then commits source snapshot, all four PCO classes, hypothesis, approved Meta, continuation, and receipt in the first checkpoint. |
| AC-02 conversation-only cold start | PASS | `test_manual_and_auto_share_checkpoint_path[auto]` starts from public conversation only, crosses the configured usage threshold, and creates the first canonical records and continuation. The `pco-memory` onboarding eval also verifies that files are optional. |
| AC-03 manual/auto equivalence | PASS | The two parameterizations of `test_manual_and_auto_share_checkpoint_path` execute the same `CheckpointEngine.request` state machine and differ only in receipt `trigger`. |
| AC-04 per-turn archive | PASS | `test_turn_archive_is_incremental_and_omits_non_public_messages` proves independent, idempotent user/assistant archival while excluding system/tool messages. The OpenCode adapter also excludes synthetic `[PCO_CONTROL]` command templates so they cannot become user evidence. |
| AC-05 optional reasoning | PASS | `test_reasoning_is_not_fabricated_or_saved_when_disabled` and `test_reasoning_is_archived_but_not_indexed_or_context_injected` cover absent/disabled and exposed reasoning. |
| AC-06 post-compact context | PASS | `test_manual_and_auto_share_checkpoint_path` proves one publication before one compact; `test_reasoning_is_archived_but_not_indexed_or_context_injected` proves rendered context contains approved continuation but no old raw transcript/reasoning; `test_opencode_117_http_contract_and_worker_reclamation` proves the stable 1.17.18 `/session/:id/summarize` contract and real `info.structured` response shape; `test_compaction_retry_detects_completed_native_summary` prevents duplicate compaction after a lost response and excludes native summaries from raw evidence. The real run described below completed worker consolidation, canonical commit, publication, native compaction, and a same-session model request that used the published continuation. |
| AC-07 worker isolation | PASS | Checkpoint tests cover input locking state, `Yes` without a second worker resume, `No` with one semantic resume, deterministic rebuild from the frozen boundary, and retryable worker reclamation. The OpenCode adapter test verifies child creation, structured JSON schema, abort, and delete calls; the consolidator config is hidden and denies edit/bash/task/question. |
| AC-08 consolidate failure | PASS | `test_validate_failure_blocks_compact_and_retry_keeps_boundary` proves no commit/cursor/compact on invalid references and successful retry over the identical frozen range. |
| AC-09 derivation failure | CONTRACT PASS; LIVE PENDING | `test_affine_failure_is_reported_after_commit_and_retry_is_idempotent` proves canonical commit and Meta activation survive a missing AFFiNE bridge, the main-session receipt reports pending, and retry reaches DONE without recommit. A real AFFiNE workspace remains deployment-specific. |
| AC-10 source diff | PASS | `test_source_snapshot_and_diff_only_advance_with_transaction` covers idempotent registration, first snapshot, unchanged-source suppression, and unified diff after update. |
| AC-11 natural-language correction | PASS | `test_ac11_natural_language_correction_keeps_history_and_updates_current_meta` retains revision 1, adds disputed revision 2, updates current Meta, and excludes the disputed portrait from current retrieval. |
| AC-12 authorized promotion | PENDING LOOPBACK | Python tests cover protected bytes, host-grant validation, exact No reason/provenance, replay/expiry, dismissal, and unchanged Meta after rejection. The Bun loopback covers the fixed form, direct model bypass, child-session rejection, and matching one-time Yes/No grants; a real OpenCode deployment still needs dismissal/re-display, restart recovery, and full manual acceptance. Legacy slash-command fallbacks are not supported and must not be installed. |
| AC-13 historical understanding | PASS | `test_current_mode_excludes_old_meta_but_historical_includes_it` and `test_historical_mode_exposes_revision_policy_and_reason` distinguish current from old revisions and return the historical policy version and revision reason. |
| AC-14 external concept reference | PASS | `test_concept_requires_external_search_receipt` rejects a psychology/philosophy concept whose external reference has no search receipt. |
| AC-15 hybrid retrieval | PASS | `test_five_retrieval_modes_return_evidence_time_and_qualification`, chunk/reasoning tests, and the real-backend marker cover five modes, evidence/time/qualification fields, change windows, graph boost, turn-aware chunks, Tantivy, and Milvus Lite. |
| AC-16 Profile decoupling | PASS | `test_non_pco_profile_uses_same_core_without_code_changes` loads the bundled Research Profile, exercises `auto` and `read_only` streams plus retrieval/projection capabilities, and never changes `mem-core`. |
| AC-17 replaceable projection | CONTRACT PASS; LIVE PENDING | `test_backlinks_and_replaceable_projections_are_idempotent` projects the same commit to Markdown and the strict AFFiNE bridge contract without changing canonical Git. `test_clone_rebuilds_all_replaceable_derivations` repeats index, backlinks, Markdown, and AFFiNE-contract derivation from a fresh Git clone. A provider-specific AFFiNE bridge/instance is still required for live page creation. |

Test names are under `tests/` and can be selected directly with `pytest -q -k <name>`.

## Additional release evidence

- The generic transaction path enforces `auto`, `user_approval`, and `read_only`; approval binds the reviewed protected-operation hash, final operation-set hash, decision message, base commit, and transaction fingerprint.
- The managed Git pre-commit hook reruns Profile/schema/reference validation and rejects an invalid staged JSONL envelope; messages-only archive commits use an incremental delta fast path, structured commits still get full-tree validation.
- Canonical transaction state, checkpoint artifact, raw decision, Meta/continuation revisions, source snapshot, and receipt are Git-tracked; derived index/projection failures never roll back them.
- A fresh Git clone rebuilds the configured Milvus/Tantivy backends, backlinks, Markdown, and the AFFiNE bridge batch; backend failures remain structured and retryable.
- OpenCode 1.17.18 successfully parses the installed local plugin, hidden agent, retained commands, permissions, and `pco-memory` skill. The authorization claim additionally requires the executable question lifecycle loopback described for AC-12.
- A real loopback OpenCode 1.17.18 server completed the full checkpoint path with `opencode/north-mini-code-free`: checkpoint `ckpt_b01329d9e53f4cd98710cb295d3e42b3`, canonical commit `d03ad3a6a74a27acea869d6b7dd842b569b62abb`, `validated_json_text_repair`, native compaction, all derivations, receipt, and worker reclamation reached `DONE`. A retry after the server had already compacted detected the native summary and did not compact or commit again.
- In that same compacted session, a new model request was asked for the second `current_topics` item. It returned the exact published-context value `Distinguishing facts from personality patterns`. The awaited idle hook archived only that post-checkpoint user/assistant turn; the native compaction marker/summary was absent from raw conversation. This live path also found and fixed awaited-idle, structured-output compatibility, worker-model propagation, control-message filtering, and the unavailable v2 compact endpoint.
- The built wheel contains both Profiles and every OpenCode agent/command/plugin/skill resource.
- Three paired `pco-memory` skill evals score 100% with the skill versus 66.7% baseline. Timing/token metadata was unavailable from the executor notifications and was not invented.

## Retrieval backend note

Since the refactor removed the self-built lexical/dense fallback engines, tests that
exercise real `search`/`build_index` (AC-11, AC-15, and the retrieval projection suite)
require a loopback-capable Milvus Lite and are skipped unless `PCO_RUN_MILVUS=1` is set.
Backend failures now surface as `INDEX_BACKEND_FAILED` with recovery hints instead of
silent degradation.

## Performance baseline

PRD §25.4-scale benchmark (100k messages / 10k events / 5k concepts / 1k sources) is
reproducible via `scripts/benchmark_corpus.py`; results and deviation analysis are in
[PERFORMANCE.md](PERFORMANCE.md). Current numbers exceed the 10 s validate/commit and
2 s retrieval targets; the dominant cost is full-corpus validation inside
`TransactionManager.validate`, which is the next optimization item.

## Commands

```bash
pytest -q
PCO_RUN_MILVUS=1 pytest -q -m milvus

# Build both distributions from the monorepo:
python -m pip wheel packages/mem-core --no-deps --no-build-isolation -w /tmp/pco-wheel-check
python -m pip wheel packages/pco --no-deps --no-build-isolation -w /tmp/pco-wheel-check

# Run inside a temporary project after `pco install-opencode`:
opencode debug config
opencode debug skill
```

## Remaining deployment gates

Repository conformance is not the final operational release claim. Environment-dependent checks remain before declaring the PRD's full MVP release gate complete:

1. Supply a provider-specific `PCO_AFFINE_COMMAND` for an actual AFFiNE deployment, project a canonical commit, inspect the created pages/links/backlinks in AFFiNE, and repeat the same batch to prove target-side idempotence.
2. Run the OpenCode question loopback for manual Yes, manual raw-reason No, dismissal/re-display, direct model bypass, and plugin restart. Until then, describe authorization as contract coverage, not live PASS.

The repository cannot manufacture an AFFiNE workspace or its deployment credentials. Until that check is supplied, describe the result as “MVP implementation with live OpenCode acceptance and AFFiNE contract coverage,” not “production-accepted MVP.”
