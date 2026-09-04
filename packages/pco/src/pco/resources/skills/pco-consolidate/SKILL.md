---
name: pco-consolidate
description: Use when new evidence, corrections, or durable conversation insights should become a reviewable PCO proposal, or when an active proposal must be resumed after denial or in a fresh session.
---

# PCO Consolidate

Stage a transparent, complete proposal before asking to change canonical memory.
Proposal authoring and canonical submission are separate Host permission boundaries.

## Workflow

1. In every session, first run `pco verify` and `pco proposal status`. These are
   read-only. In a fresh session, inspect and resume the reported active proposal;
   do not replace it with a new scaffold.
2. Search canonical memory and history for existing objects and revisions. Inspect
   relevant source changes before deciding what should change.
3. If no proposal is active, run `pco proposal scaffold`. It is read-only and
   prints starting manifest data; it does not create or reserve a proposal.
4. Plan the entire closed proposal set using
   [references/proposal-format.md](references/proposal-format.md). Include the
   manifest, every proposed business Markdown file, and every input artifact.
5. **Always explain the complete intended proposal before Edit.** Describe every
   semantic operation, its evidence, uncertainties, and all files to be authored.
6. Request one-time write permission for the ordinary Host Edit, then author the
   complete proposal through that Edit surface. The visible Edit diff is the first
   review surface. If denied, stop; do not use another write path.
7. Run `pco proposal validate <review_id>`. Validation is read-only and advisory.
   Resolve errors by revising the complete proposal through another explained Host
   Edit, then validate again. Validation is not approval.
8. After the user has reviewed the complete proposal, use exactly
   `pco proposal commit <review_id>` for canonical submission. This command crosses
   the second Host permission boundary; the earlier Edit permission does not grant
   it.
9. If commit permission is denied, keep the proposal editable and stop. The denial
   leaves canonical memory unchanged. In a later or fresh session, repeat verify
   and status, inspect the same active proposal, and resume from validation or
   review.

Never describe validation, an Edit result, or tool output as machine proof of
approval. Report only the Host permission result and the command's actual status.

## Permission contract

Read [PERMISSIONS.md](PERMISSIONS.md) before requesting either boundary. A denied
Edit or commit never justifies broader access: permanent workspace write permission is unsupported.
