# Task 4 implementation report — exact v0.5 public CLI

## Status

Complete on baseline `3cb7dfd`; ready to commit with message
`feat: complete the v0.5 public CLI`.

Implementation scope:

- `packages/pco/src/pco/cli.py`
- `packages/pco/src/pco/proposals/service.py`
- `tests/conftest.py`
- `tests/test_v05_cli.py`
- `tests/test_v05_cli_workspace.py`
- `tests/test_proposal_validation.py`

The proposal-service and existing workspace-test edits are the controller-approved
resolution of two Task 4 boundary issues: distinguishing an absent active proposal
from a mismatched active proposal, and replacing Task 1's transitional `verify`
exit-zero expectation with Task 4's final failure status.

## Implementation

- Exposes exactly eight public top-level commands: `init`, `doctor`, `verify`,
  `search`, `history`, `source`, `proposal`, and `derive`.
- Exposes only `source diff`; proposal `status`, `scaffold`, `validate`, `commit`,
  and `abort`; and `derive all`. No v0.4 command was restored.
- Wires the complete argument surface: optional init/verify/source/search values,
  search mode/limit/time bounds, history stream/record ID, proposal review IDs,
  and the explicit modified-projection discard flag.
- Dispatches through the existing workspace, retrieval, source, proposal, and
  derivation services. History reads canonical record history directly and no read
  command prepares or repairs derivations.
- Emits exactly one compact, sorted JSON object on stdout. A successful result
  returns zero; a service result with `ok: false`, a known `MemError`, invalid input,
  or an unexpected exception returns one without an unstructured traceback.
  Canonical proposal success remains exit zero when archival is only `pending`.
- `ProposalService.validate` now returns `PROPOSAL_NOT_FOUND` only when there is no
  active proposal. One active proposal with another ID remains
  `PROPOSAL_MANIFEST_MISMATCH`, and multi-active integrity failure remains owned by
  `active_proposals`.
- Adds independent function-scoped history and active-proposal fixtures. Each fresh
  workspace contains only the evidence-backed hypothesis state needed by its CLI
  read and never shares mutable workspace state with the empty read-command cases.

## RED / GREEN evidence

- Service boundary RED: the new no-active validation test expected
  `PROPOSAL_NOT_FOUND` but observed `PROPOSAL_MANIFEST_MISMATCH`. After the narrow
  domain fix, no-active and wrong-ID cases passed together (`2/2`).
- CLI RED: `tests/test_v05_cli.py` collected 16 tests; 12 failed because the five
  missing top-level commands could neither parse nor dispatch, while four existing
  init/doctor/error-envelope behaviors passed.
- CLI GREEN: after minimal parser and dispatcher wiring, all 16 passed.
- Planned focused transitional RED: the combined 26-test CLI suite had exactly two
  failures because existing missing-projection tests still expected exit zero.
  Updating those Task 4-owned status assertions produced `26/26` passing.
- Post-format focused/service run: `67/67` passed. After restoring unrelated
  legacy-file formatting, the final CLI plus two service-boundary tests passed
  again (`28/28`).
- CLI and consumed-service combination: `350/350` passed (CLI, proposal, source
  diff, retrieval, derivation, and current projection).

Argparse's own unknown-command/usage `SystemExit` behavior remains unchanged. The
single-JSON contract is tested at the requested boundary: successfully parsed known
business failures and unexpected dispatch exceptions.

## Final verification

- Normal full suite: 741 collected; 737 passed and 4 expected real-backend skips.
- `PCO_RUN_MILVUS=1` full suite: 741/741 passed.
- `python -m py_compile` on both touched production modules: exit 0.
- Black check on both production modules, `conftest.py`, the new CLI test file,
  and the existing CLI workspace test: clean. The 14-line service regression was
  formatted before restoring unrelated Black changes in its legacy test file.
- `git diff --check`: exit 0.
- Fresh local `mem-core` and `pco` wheels installed with `--no-deps` into a new
  system-site-packages venv. The installed `pco` import resolved from that venv's
  `site-packages`; `pco --help` listed the exact eight-command tree; wheel-installed
  `pco init` and `pco doctor` both returned compact successful JSON.
- Fresh `pco-0.5.0-py3-none-any.whl`: 90/90 unique members. `pco/cli.py` and
  `pco/proposals/service.py` each occur once and are byte-identical to the worktree.
  - CLI SHA-256: `8edbb40b01b90945c984c359d7150e0ee39005ae03d718937f84f7e1d986b395`
  - proposal service SHA-256:
    `12c929ae82d7ea520258c1e61bb229ed81d88faadd0cd99c90f8cc3499f930ce`

Both full suites emitted the same existing Python 3.13 multiprocessing fork
deprecation warning in one workspace-init test; it did not affect exit status and
is unrelated to this task.

## New rulings

1. CLI exit status follows the returned command envelope's top-level `ok`, not only
   whether dispatch raised. If wrong, the cost is changing one final status branch;
   treating structured verification/validation failure as shell success breaks
   automation.
2. An absent active proposal is `PROPOSAL_NOT_FOUND`; a present but different active
   review is `PROPOSAL_MANIFEST_MISMATCH`. If wrong, the cost is collapsing the two
   branches again, but doing so removes the recovery-relevant distinction required
   by the public error contract.
3. Parser usage errors remain argparse-owned; the compact JSON error contract begins
   after successful parsing. If wrong, the cost is introducing a custom parser error
   boundary, which is not required by the approved Task 4 plan.

## Blockers

None.
