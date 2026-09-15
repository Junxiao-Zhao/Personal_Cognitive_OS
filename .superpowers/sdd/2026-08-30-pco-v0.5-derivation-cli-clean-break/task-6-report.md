# Task 6 implementation report

## Scope

Implemented the final v0.5 acceptance and release-gate surfaces:

- end-to-end proposal acceptance for cold start, correction, denial, derivation
  failure, and idempotent retry;
- clean-wheel orchestration plus recording-runner contract tests;
- the manual Codex/OpenCode TUI permission smoke protocol;
- README release-verification entry points.

No product implementation change was required.

## TDD record

### Acceptance

The first acceptance run was RED with three passes and one failure. The
correction assertion expected an unframed body string, while the strict
Markdown parser correctly preserves the newline framing after frontmatter
(`"\n...\n"`). The test expectation was corrected to the established parser
contract; no product code changed.

Final focused result:

```text
python -m pytest tests/test_v05_acceptance.py -q
.... [100%]
```

The four acceptance cases use real proposal directories, manifests, business
Markdown, proposal-artifact input, `ProposalService.commit`, canonical Git,
and derivation. They prove:

- a cold-start transaction commits all nine v0.5 business streams and a
  verified current projection;
- a hypothesis correction retains revisions 1 and 2 and updates current state;
- Host commit denial is modeled by not invoking the command, preserving the
  active proposal and canonical HEAD;
- injected derivation failure does not undo canonical success, and retry finds
  the same receipt and leaves exactly one transaction.

### Clean-wheel smoke

The recording-runner test was first run before the production script existed.
Collection failed with the expected import error for
`scripts.smoke_clean_wheels`.

After implementing the script:

```text
python -m pytest tests/test_clean_wheel_smoke.py -q
.. [100%]
```

The contract records both source builds into one owned temporary wheelhouse,
one fresh `EnvBuilder(with_pip=True)` environment, installation arguments that
contain only the two resolved wheel paths, the isolated resource/version probe,
and both console-script help commands. It also proves both owned temporary
directories are removed and rejects duplicate matching wheels.

## Automated release evidence

- Related acceptance/smoke/SKILL/proposal/derivation/CLI suites: 92/92 passed.
- Clean-break, repository inventory, SKILL, and README authoritative-link
  suites: 35/35 passed.
- Normal full suite: 758 passed, 4 opt-in tests skipped, 0 failed/errors.
- `PCO_RUN_MILVUS=1` full suite: 762/762 passed, 0 skipped/failed/errors.
- Both full runs emitted only the existing Python 3.13 multiprocessing fork
  deprecation warning from a workspace-init test.
- `python -m black --check` on all three new Python files: clean.
- `python -m py_compile` on all three new Python files: clean.
- `python -m compileall -q packages/mem-core/src packages/pco/src`: clean.
- `git diff --check`: clean.

The actual command below completed with exit status 0:

```text
python scripts/smoke_clean_wheels.py
```

It built exactly `mem_core-0.5.0-py3-none-any.whl` and
`pco-0.5.0-py3-none-any.whl` in a fresh temporary wheelhouse, created a fresh
venv, installed those two wheel paths (not either source directory), installed
their declared dependencies, asserted both package versions are `0.5.0`,
enumerated the bundled Profile and both SKILL resource trees through
`importlib.resources`, and ran `pco --help` plus `mem --help`. The script ended
with `clean-wheel smoke passed`; its two `TemporaryDirectory` contexts removed
their owned paths.

## Manual Host gate

`docs/verification/pco-v0.5-tui-smoke.md` is an executable protocol for fresh,
separate Codex and OpenCode workspaces. It includes observational fields for
actual Host versions and prompt counts and exercises SKILL discovery,
verify-before-projection-read, canonical fallback, read-only commands,
explanation-before-Edit, denial and retry at both logical boundaries,
review-ID-only commit, no permanent grant, fresh-session proposal resume,
canonical success during an injected derivation failure, and explicit recovery.

The manual Codex/OpenCode gate was **not executed** in this non-interactive
implementation environment. Its Host versions, prompts, observations, and pass
status remain deliberately unfilled. Automated tests do not substitute for or
claim completion of this sole cross-Host Agent-behavior gate.
