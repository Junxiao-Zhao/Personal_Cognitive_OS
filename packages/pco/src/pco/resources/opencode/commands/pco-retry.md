---
description: Retry checkpoint recovery or pending post-commit derivations
subtask: false
---

[PCO_CONTROL] Call `pco_retry` exactly once and show the result. If it returns `AWAITING_META_APPROVAL`, call the native `question` tool immediately in this same turn so the plugin can bind a fresh question request to the durable proposal.
