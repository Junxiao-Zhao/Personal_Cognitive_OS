# Task 3 fix round 1 report — closed whole-tree cleanup authorization

## Status and scope

The single Important finding from `task-3-review-1.md` is fixed on baseline
`244edb8`. The change is limited to:

- `packages/pco/src/pco/derivation.py`
- `tests/test_derive_all.py`
- this report

The review's non-blocking final-source rename observation was intentionally not
expanded or changed.

## Finding and RED

Recursive cleanup previously captured only the current directory. Opening an
already-authorized child and recursively calling the same helper created a fresh
authorization table from the child's later contents. The retained review regression
inserted `post-capture.txt` immediately after that child open.

Before the product change:

- `pytest -q tests/test_derive_all.py::test_recursive_cleanup_never_authorizes_a_nested_post_capture_name`
  failed at `pytest.raises` because cleanup returned success and deleted the inserted
  name (`1 failed`, exit 1).

The unit's descriptor assertion is intentional: its baseline includes the caller's
retained `root_fd`, which the test closes before comparing, so the expected final
count is exactly one lower. Any leaked capture descriptor makes the assertion fail.

## Implementation

`_erase_owned_directory()` now has three distinct phases:

1. `_capture_owned_tree()` recursively captures the complete tree without removal.
   Every directory has a retained descriptor plus identity/version, and every
   descendant name has one immutable `(device, inode, type)` token. Directory-open
   and recursive capture failures permanently reject that entry. Capture poststate
   detects additions, disappearance, replacement, and unstable directory metadata.
2. `_validate_owned_tree_snapshot()` rechecks the whole retained snapshot before the
   first destructive operation. It never adds an authorization. Missing, changed,
   or unreadable entries are rejected while independent captured siblings remain
   eligible for cleanup.
3. `_execute_owned_cleanup()` consumes only the snapshot. Its `listdir()` calls only
   detect preserved residue. Each destructive entry boundary first uses an
   unpredictable same-parent `RENAME_NOREPLACE` detach and validates source/detached
   poststate before unlink or `rmdir`. Directory recursion uses only the descriptor
   retained during capture and never recaptures or retries a failed subtree.

Snapshot descriptors are closed recursively in one outer `finally`, including
capture, validation, execution, and failure paths. Symlinks and special entries are
never followed; replacements and failed tombstones are preserved. Cleanup still
continues through independently authorized siblings and reports one bounded
`OSError`, which the public flow normalizes as `DERIVATION_FAILED` where applicable.

Both production cleanup paths use the same whole-tree helper:

- process-root cleanup in `_owned_process_root()`;
- detached public-target cleanup in `_publish_output()`.

Normal successful derivation still removes its process root completely, and prior
crash remnants remain outside the invocation's cleanup scope.

## Regressions and GREEN

The retained unit regression proves the shared helper preserves a nested name added
after its parent authorization, erases an independent owned sibling, raises bounded
`OSError`, and closes every retained child descriptor.

One integrated regression exercises public `derive_all()`. It injects a publication
failure, observes the real process-root detach, then inserts a nested name when
whole-tree capture opens `PCO_MEMORY`. The result is structured
`DERIVATION_FAILED`; the nested payload and failed subtree survive, the captured
`indexes` sibling is erased, and the process descriptor count returns to baseline.

- Both cleanup regressions: `2 passed`, exit 0.
- Derivation file: `39 passed`, exit 0.
- Formatted focused suite (`derive_all`, proposal commit, current projection):
  `92 passed`, exit 0.
- Task 1–3/proposal combination: `217 passed`, exit 0.

## Final verification

- Normal full suite: `724 collected`; `720 passed`, `4` expected real-backend skips,
  exit 0.
- `PCO_RUN_MILVUS=1` full suite: `724/724 passed`, exit 0.
- Real Tantivy/Milvus backend gate: `4/4 passed`, exit 0.
- `python -m py_compile` on the two changed Python files: exit 0.
- `black --check --no-cache --workers 1` on the two changed Python files: both left
  unchanged, exit 0.
- `git diff --check`: exit 0.
- Fresh `pco-0.5.0-py3-none-any.whl`: build and ZIP integrity passed; 90 members,
  90 unique, no duplicates. Packaged `pco/derivation.py` and
  `pco/proposals/service.py` were raw-byte identical to source.
  - derivation SHA-256:
    `6cd5ff6f5eaff7c221da5065691bc7a25710f5de691f2c736d1ef6784bb2177d`
  - proposal service SHA-256:
    `7097597542b95c9fbb945ba5d39ef1c46c94678cff3023fd0d32951172996429`

Both full suites emitted only the existing Python 3.13 multiprocessing `fork()`
deprecation warning from the workspace-init FIFO test. The disposable wheel build
directory was removed after payload verification.
