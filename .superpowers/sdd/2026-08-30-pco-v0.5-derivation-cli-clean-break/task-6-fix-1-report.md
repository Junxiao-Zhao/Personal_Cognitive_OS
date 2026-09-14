# Task 6 fix round 1 implementation report

## Scope

Closed both Important findings from `task-6-review.md` without changing the
acceptance flows, Host protocol, product packages, or documentation:

- every build, install, probe, and help subprocess now receives one explicitly
  sanitized environment and runs from the owned, resolved wheelhouse outside
  the repository;
- the installed probe rejects `mem_core` or `pco` origins outside the fresh
  environment's `purelib`/`platlib`, and loads and validates the bundled v0.5
  Profile as well as enumerating both SKILL trees;
- the recording test freezes the exact ordered six-command contract, exact
  interpreter/wheel/console-script paths, common environment, common cwd, and
  owned temporary-directory cleanup;
- wheel resolution coverage now includes normalized requested distribution
  names and missing, duplicate, and wrong-version requested distributions.

The child environment removes `PYTHONPATH`, `PYTHONHOME`, `PYTHONUSERBASE`,
`PYTHONSTARTUP`, `PYTHONINSPECT`, `PYTHONEXECUTABLE`,
`__PYVENV_LAUNCHER__`, and `VIRTUAL_ENV`, then sets
`PYTHONNOUSERSITE=1`. Other variables remain available, including pip index,
proxy, and certificate configuration required for normal dependency install.

## TDD evidence

The regression tests were written before the smoke implementation changed.
Against `d8c4c16`, the first focused run collected eight tests and produced the
expected two failures:

- the exact orchestration test failed because the old smoke calls did not pass
  the recording runner's required `env` and `cwd` arguments;
- a real interpreter launched from an external cwd with `PYTHONPATH` pointing
  at both worktree source trees returned zero, proving the old probe accepted
  source imports.

The other six cases already passed, including the resolver boundaries and
POSIX/Windows command-path cases. After the minimal implementation and Black
formatting:

```text
python -m pytest tests/test_clean_wheel_smoke.py -q
........ [100%]
```

## Clean-wheel evidence

`python scripts/smoke_clean_wheels.py` exited zero. It built exactly the
mem-core and PCO 0.5.0 wheels, installed their wheel paths in a fresh venv,
validated both installed module origins, loaded the installed Profile, checked
all nine streams and both SKILL resource trees, ran installed `pco --help` and
`mem --help`, printed `clean-wheel smoke passed`, and removed both owned
temporary directories.

A second real run started Python normally and only then injected worktree
source paths into `PYTHONPATH`, an invalid `/definitely-invalid-python-home`
into `PYTHONHOME`, and an unrelated `VIRTUAL_ENV`. Calling `smoke()` from that
polluted process also exited zero and printed `polluted-parent smoke passed
safely`; the child build/install/probe/help interpreters therefore did not
inherit those values. This setup intentionally avoids putting invalid
`PYTHONHOME` around the parent Python launcher, which would fail before the
script could sanitize child environments.

## Automated verification

- Focused clean-wheel tests: 8/8 passed.
- Acceptance plus clean-wheel tests: 12/12 passed.
- Clean-break, inventory, and SKILL tests: 35/35 passed.
- `python -m pytest --collect-only -q`: 768 tests collected.
- Normal full suite: exit 0, 764 passed and 4 opt-in tests skipped, no
  failures/errors.
- `PCO_RUN_MILVUS=1` full suite: exit 0, 768 passed, no
  skips/failures/errors.
- Both full runs emitted only the existing Python 3.13 multiprocessing fork
  deprecation warning from a workspace-init test.
- Black check on the modified script and test: clean.
- `py_compile` on the modified script and test: clean.
- `git diff --check`: clean.
- README/manual-protocol relative links: 7 checked, 0 missing.

## Manual Host gate

The separate Codex/OpenCode TUI permission protocol remains **NOT RUN**. This
fix does not fabricate Host versions, prompt counts, transcripts, or a release
pass; those observations still require the documented manual runs.
