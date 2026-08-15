---
description: Reject the pending PCO Meta-memory proposal; a non-empty reason is required after the command
subtask: false
---

[PCO_CONTROL] The user's objection or supplemental experience is: `$ARGUMENTS`

If the text after the command is empty, do not call any tool and state that `/pco-no` requires the reason in the same command. Otherwise call `pco_reject` exactly once with that text. Show the resulting checkpoint receipt and do not ask a follow-up question.
