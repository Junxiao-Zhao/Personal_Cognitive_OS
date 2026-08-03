---
description: Run a PCO memory checkpoint, publish approved context, then compact
subtask: false
---

[PCO_CONTROL] Call `pco_checkpoint` exactly once. If it commits without approval, show its receipt. If approval is required:

1. Show the exact protected Meta diff, main evidence, and proposal hash.
2. Call OpenCode's `question` tool in this main session with one single-choice question, `header` = `Meta approval`, options `Yes` and `No`, and `custom` = true. Explain that `Yes` approves the exact hash and that a rejection must use the custom/Other input (Tab in the TUI) to enter a non-empty objection or supplemental experience.
3. If the answer is `Yes`, call `pco_approve` exactly once. If it is non-empty custom text, call `pco_reject` exactly once with that complete text. A bare `No` has no valid reason: show the same form again and do not call `pco_reject` until custom text is non-empty.
4. Show the final checkpoint receipt. After a rejection, do not ask another follow-up.

Do not call any native compact command yourself.
