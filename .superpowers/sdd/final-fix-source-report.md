# Final branch fix report — removed source runtime

## Scope and outcome

Addressed final branch review Important finding 1 only. The installed
`pco.sources` module now retains the v0.5 read-only `source_diff` surface and
no longer defines the v0.4 direct registration/materialization runtime.

Removed symbols:

- `SourceRead`
- `SourceReader`
- `normalize_text`
- `content_hash`
- `_validate_locator`
- `read_local_file`
- `ReaderRegistry`
- `SourceManager`
- `SourceReaderRegistry`

The only production file changed is `packages/pco/src/pco/sources.py`. Tests
changed are `tests/test_source_diff_v05.py` and `tests/test_clean_break.py`.
Proposal and retrieval files were not touched by this fix.

## TDD evidence

Before changing production code, source-tree and freshly built wheel-facing
negative assertions were added for every removed symbol. Against `f20faf7`,
the exact RED selection failed 2 tests and passed the 3 command/capability
cases. Both failures enumerated all nine legacy symbols as still present:

`pytest -p no:cacheprovider -q tests/test_clean_break.py -k 'direct_source_runtime or source_write_commands'`

After deleting the legacy block and unused imports, the source diff plus clean
break suite passed 52/52. The compatibility-only
`SourceManager.materialize` monkeypatch was removed while the direct
`read_regular_file` trap remains and proves remote diff performs no material
read.

## Final verification

- Final combined targeted suite: 82/82 passed across
  `test_source_diff_v05.py`, `test_clean_break.py`,
  `test_clean_break_inventory.py`, `test_registry_discovery.py`,
  `test_v05_cli.py`, and `test_v05_cli_workspace.py`.
- `python -m py_compile` passed for the three changed Python files.
- Black check passed for the three changed Python files.
- `git diff --check` passed.
- Production/package scan found no legacy source runtime definitions,
  registry references, source-reader capability group, or source
  add/register/materialize capability.
- A fresh `pco-0.5.0` wheel was built, installed with `--no-deps` into an
  isolated temporary target, and imported under `python -I`. The observed
  `pco.sources` origin was the isolated install, all nine removed symbols were
  absent, and entry points were exactly console `pco` plus capabilities
  `pco.retrieval.search` and `pco.validate`.

The full repository suite was intentionally left to the coordinator, as
requested. Changes remain unstaged and uncommitted.
