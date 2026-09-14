# Task 6 fix round 2 implementation report

## Scope

Closed the remaining Important test-contract finding from
`task-6-fix-1-review.md`. Production `scripts/smoke_clean_wheels.py` was not
changed: its runtime behavior was already correct. This round changes only the
clean-wheel tests and this report.

The test suite now executes both the production embedded probe and the probe
captured from the exact six-command smoke orchestration against a controlled,
independently specified installed-environment scenario. The harness creates:

- a temporary fake `purelib` with real `mem_core` and `pco` origin files;
- independent fake modules, versions, Profile loader, registry, and bundled
  Profile locator in `sys.modules`;
- a real temporary resource tree containing a Profile, schema, and all three
  required files for both `pco-memory` and `pco-consolidate`;
- controlled `importlib.resources.files` and `sysconfig.get_paths` results.

All module and standard-library substitutions use pytest's `monkeypatch` and
are restored at test teardown. No process-global fake remains after a case.

## Independent probe contract

The complete controlled install passes. Separate negative cases prove that the
probe rejects each of these independently:

- `mem_core` origin outside installed `purelib`;
- `pco` origin outside installed `purelib`;
- wrong `mem_core` version;
- wrong `pco` version;
- wrong Profile name;
- wrong Profile version;
- one missing approved stream;
- one additional legacy stream;
- each of `SKILL.md`, `PERMISSIONS.md`, and `manifest.json` missing from
  `pco-memory`;
- each of those three required files missing from `pco-consolidate`.

The wheel resolver success fixture now also proves that requested names
`MEM.Core` and `PcO` and requested version `0.5` normalize to the built 0.5.0
wheels while an unrelated valid wheel remains ignored.

## Mutation RED and restored GREEN

Before this round, the focused suite was 8/8 green even under the reviewer's
four in-memory probe mutants, establishing the coverage RED/mutation gap.

After adding the controlled harness, four representative production checks
were temporarily removed one at a time and immediately restored with
`apply_patch`. Each corresponding new pytest node failed as intended with
`Failed: DID NOT RAISE <class 'AssertionError'>`:

- `require_installed_origin(mem_core)`;
- the joint package-version assertion;
- the Profile-name assertion;
- the SKILL required-files assertion.

After restoring every production line, the scoped production-script diff was
empty and the focused suite was green:

```text
python -m pytest tests/test_clean_wheel_smoke.py -q
....................... [100%]
```

## Verification evidence

- Focused clean-wheel tests: 23/23 passed.
- Acceptance plus clean-wheel tests: 27/27 passed.
- Clean-break, inventory, and SKILL tests: 35/35 passed.
- Normal real clean-wheel smoke: exit 0.
- Polluted-parent real clean-wheel smoke with worktree `PYTHONPATH`, invalid
  child `PYTHONHOME`, and unrelated `VIRTUAL_ENV`: exit 0.
- `python -m pytest --collect-only -q`: 783 tests collected.
- Normal full suite: exit 0, 779 passed and 4 opt-in tests skipped, no
  failures/errors.
- `PCO_RUN_MILVUS=1` full suite: exit 0, 783 passed, no
  skips/failures/errors.
- Both full runs emitted only the existing Python 3.13 multiprocessing fork
  deprecation warning from a workspace-init test.
- Black check and `py_compile` for the modified test: clean.
- `git diff --check`: clean.
- README/manual-protocol relative links: 7 checked, 0 missing.
- Production smoke-script diff: empty.

## Manual Host gate

The separate Codex/OpenCode TUI permission protocol remains **NOT RUN**. No
Host version, prompt count, transcript, or release result is claimed by these
automated tests.
