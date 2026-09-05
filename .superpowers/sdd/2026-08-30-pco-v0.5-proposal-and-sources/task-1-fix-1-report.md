# Proposal Task 1 fix round 1 report

## Scope

- Fixed only the three findings in `task-1-review.md` on top of
  `0ad1fd0cc68bb113cf8ef36964b4c78777f73bf7`.
- Preserved the proposal scaffold/status API and its read-only behavior.
- Did not modify the SDD progress ledger or implement Proposal Task 2+.

## RED evidence

The original implementer added review regressions before changing production
code. Its first focused run collected 54 cases and reported 22 failures:

- four unsafe `.committed` forms were silently excluded;
- a proposal-root swap selected the attacker's child and no root descriptor
  lifecycle was observable;
- five incompatible/invalid string versions raised Pydantic
  `ValidationError` instead of the required `MemError`;
- the bundled schema required runtime-defaulted fields, rejected compatible
  PEP 440 spellings, and did not express array uniqueness.

A separate non-string version run failed 4/4 because Pydantic rejected the
input before the compatibility contract could return a structured error.

During takeover audit, the new compatibility sample set exposed two remaining
schema/runtime mismatches. This command failed 2 of 16 cases because runtime
accepted `0.5post` and `0.5.0_rev_1` while the schema regex rejected them:

```text
pytest tests/test_proposal_scaffold.py::test_manifest_accepts_every_compatible_pep440_version_sample tests/test_proposal_scaffold.py::test_schema_accepts_compatible_pep440_version_samples -q
............FF..
```

A final bidirectional syntax audit then added whitespace-padded versions and
the invalid implicit-post spelling `0.5_1`. The paired runtime/schema command
failed 4 cases: runtime tolerated leading/trailing whitespace, the schema
tolerated `_N`, and Python-regex `$` admitted a trailing newline. The same
command passed 16/16 after tightening both sides. A generated matrix of 1,120
representative base/post/local/whitespace spellings then found zero acceptance
differences between runtime and schema.

## Fixes

- `ProposalManifest.schema_version` now validates in `mode="before"` and maps
  malformed, incompatible, and non-string inputs to `MemError` code
  `WORKSPACE_FORMAT_MISMATCH`, including the observed value and supported
  package range. Surrounding whitespace is rejected rather than relying on
  `packaging.Version`'s input tolerance.
- Active proposal discovery opens the proposal root exactly once with
  `O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`, scans through that descriptor, inspects
  each immediate child without following links, validates `.committed` as a
  real directory before excluding it, and closes the descriptor on success or
  structured failure. A pathname swap cannot redirect enumeration to an
  attacker-controlled replacement.
- The JSON Schema now mirrors runtime defaults instead of requiring those
  fields, accepts compatible final/post PEP 440 v0.5 spellings, rejects the
  exercised invalid/incompatible spellings, distinguishes valid `-N` implicit
  posts from invalid `_N`, anchors the absolute string end, and uses
  `uniqueItems` for exact duplicates. `x-pco-supported-range` records the authoritative packaging
  range and `x-pco-unique-by` documents property-level uniqueness that generic
  JSON Schema cannot express.
- Runtime/schema parity tests cover defaults, strict shapes, exact duplicates,
  property-level uniqueness extensions, compatible versions, invalid versions,
  descriptor lifecycle, root replacement, and unsafe reserved entries.

## GREEN and verification evidence

- The takeover parity regression passed **16/16** after the minimal regex fix.
- `pytest tests/test_proposal_scaffold.py tests/test_workspace_layout.py tests/test_workspace_verify.py tests/test_versioning.py -q`
  passed **106/106**.
- `pytest -q` reached **100%** with no failures.
- `pytest -o addopts='' --collect-only -q` reported **274 tests collected**.
- `python -m build packages/pco --wheel` succeeded and built exactly
  `pco-0.5.0-py3-none-any.whl`.
- Archive audit found exactly the four source proposal modules
  (`__init__.py`, `models.py`, `paths.py`, `service.py`), verified the bundled
  proposal schema was byte-for-byte identical to its source, and found zero
  `__pycache__`, `.pyc`, or `.pyo` entries.
- `git diff --check` was silent. The generated wheel and empty `dist`
  directory were removed after inspection.

## Remaining concerns

- A generic JSON Schema validator cannot execute `packaging.SpecifierSet` or
  enforce uniqueness by selected object properties. The schema therefore
  exposes explicit `x-pco-supported-range` and `x-pco-unique-by` extensions;
  PCO runtime validation remains authoritative for those two semantics.
- Descriptor-anchored discovery prevents root-swap redirection during the
  scan. Returned values remain the existing `Path` API; later operations must
  independently retain the same no-follow/identity discipline rather than
  treating a previously returned pathname as an open capability.
