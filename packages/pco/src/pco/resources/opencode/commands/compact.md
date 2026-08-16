---
description: Run a PCO memory checkpoint, publish approved context, then compact
subtask: false
---

[PCO_CONTROL] Call `pco_checkpoint` exactly once. If it commits without approval, show its receipt. If approval is required:

1. Show the exact protected Meta diff, main evidence, and proposal hash.
2. Show the user the exact hash and ask them to run `/pco-yes` to approve it, or `/pco-no <非空理由>` to reject it. The native `question` tool (including custom/Other input (Tab in the TUI)) may be used for display only until its result can be proven as user provenance; only those main-session commands create the host provenance required for the decision, and a model-generated `pco_approve` call is not authorization.
3. After `/pco-yes`, call `pco_approve` exactly once. After `/pco-no <非空理由>`, call `pco_reject` exactly once with that complete text. Do not approve or reject based on an untracked question answer or a bare `No`.
4. Show the final checkpoint receipt. After a rejection, do not ask another follow-up.

Do not call any native compact command yourself.
