# Task 5 implementation report — v0.5 clean break

## Outcome

Removed the v0.4-only runtime, Host-specific packaged resources, transitional
capability adapters, superseded product documents, and their one direct
Host-only executable test. Retained the generic mem-core/Profile/registry
tests and all v0.5 canonical, proposal, source, projection, retrieval,
derivation, and CLI services.

The package now exposes only the `pco.validate` and `pco.retrieval.search`
mem-core capabilities. The README and accepted ADR describe the dedicated
workspace, proposal-first workflow, canonical-only transaction boundary, no
migration premise, and the approved v0.5 design and four implementation plans.

## Inventory evidence

- Starting commit: `eb2f3672d79606de90285ce911c6b7ee09d5a336`.
- The deletion set matched Task 5's enumerated runtime, resource, and document
  paths. The only deletion not literally named in the file list was
  `tests/opencode_question_loopback.ts`, the directly obsolete executable test
  for the removed packaged OpenCode plugin, as approved by the ledger ruling.
- No deleted `pco.archive`, `pco.context`, `pco.harness`, `pco.checkpoint`,
  `pco.repo_loader`, or `pco.research_profile` module remains imported by
  production code.
- The plan's forbidden Host/runtime term scan over `packages/pco/src` and
  `packages/pco/pyproject.toml` returned no matches.
- All six README links to the approved design, ADR, and four implementation
  plans resolve in the repository; superseded document links are absent.
- The retained Profile resource contains only the v0.5 profile and its nine
  current schemas.

## Verification evidence

- Clean-break and registry focused gate: `17/17` passed after final formatting.
- Generic current/Profile/retrieval preservation gate: `98/98` passed before
  the full run.
- Normal full suite, rerun from zero after takeover: `754` tests collected,
  exit status `0`, no failures or errors, with the expected four opt-in skips
  observed. The doubled quiet setting placed the sole warning summary in the
  retained tail instead of the numeric pass line; no inferred pass count is
  presented as direct output.
- Milvus opt-in full suite: the same `754`-test collection reached `100%`, exit
  status `0`, with no failures or errors.
- `python -m compileall -q packages/pco/src tests`: passed.
- Black check over all seven Task 5 Python change files: passed. A whole-tree
  exploratory check identified 23 unrelated pre-existing unformatted files;
  they were intentionally left untouched. The one Task 5 file it identified,
  `tests/test_clean_break.py`, was formatted and rechecked.
- `git diff --check`: passed.
- ADR existence/document gate and forbidden runtime scan: passed.

## Fresh-wheel evidence

- Built `pco-0.5.0-py3-none-any.whl` with no isolation.
- Wheel contains exactly `52` unique members and no member containing
  `opencode`, `affine`, or `checkpoint`.
- Installed the wheel without dependencies into a fresh temporary virtual
  environment; `pco.__file__` resolved inside that environment's
  `site-packages`, not the source checkout.
- Entry points were exactly console `pco = pco.cli:main` plus retained
  capabilities `pco.retrieval.search` and `pco.validate`.
- Wheel-installed `pco --help`, `pco init <new-workspace>`, and `pco doctor`
  all exited successfully; doctor returned structured `ok: true` with both
  workspace skills valid.
- The disposable wheel/venv/workspace directory was removed after smoke.
