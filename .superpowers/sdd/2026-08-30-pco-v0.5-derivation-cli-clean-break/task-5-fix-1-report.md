# Task 5 fix round 1 report — remaining v0.4 artifacts

## Finding addressed

Closed the independent review's single Important finding by removing exactly:

- `scripts/benchmark_corpus.py`, the broken v0.4 benchmark executable;
- `tests/fixtures/affine_bridge.py`, the orphan AFFiNE bridge fixture; and
- `.opencode/node_modules/` from `.gitignore`.

No production implementation or Task 6 file was changed.

## Regression-first evidence

Two exact-path tests were added before the cleanup. Against `342e564`, they
failed `2/2`: one failure listed both tracked files, and the other showed the
exact `.opencode/node_modules/` ignore line. After the bounded cleanup, those
tests passed `2/2`; the complete clean-break plus registry gate passed `19/19`.

## Verification evidence

- Adjacent clean-break inventory, generic Profile/validation, current
  projection, retrieval, and real-backend suites: `138 passed, 4 skipped`.
- Opt-in real backend suite: `4 passed in 10.36s`.
- Normal full suite: `752 passed, 4 skipped, 1 warning in 219.33s`; the warning
  is the existing multiprocessing fork deprecation warning.
- Milvus opt-in full suite: `756 passed, 1 warning in 230.75s`.
- Exhaustive residual scan found only explicit v0.5 rejection statements in
  the current README/negative tests, approved plan/spec/ADR/history reports,
  and the retained generic mem-core opaque-provenance comment. No executable,
  fixture, import, package resource, or Host namespace residual remained.
- Black on the changed test, `compileall` over package/tests, and
  `git diff --check`: passed.
- Local-link scan: `18` tracked Markdown files, zero broken local links.

## Fresh-wheel evidence

- Built `pco-0.5.0-py3-none-any.whl` with `52` unique members and no member
  containing `opencode`, `affine`, `checkpoint`, or `benchmark_corpus`.
- A fresh temporary virtual environment imported `pco` from its own installed
  wheel. Entry points were exactly console `pco`, `pco.retrieval.search`, and
  `pco.validate`.
- Wheel-installed `pco --help`, `pco init`, and `pco doctor` passed; doctor
  returned structured `ok: true` with both workspace skills valid.
- The disposable wheel, environment, and initialized workspace were removed.
