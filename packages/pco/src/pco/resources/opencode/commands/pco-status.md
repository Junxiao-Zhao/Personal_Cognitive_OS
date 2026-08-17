---
description: Show the active PCO checkpoint state
subtask: false
---

[PCO_CONTROL] Call `pco_status` exactly once and present the result without changing state. If the result is `AWAITING_META_APPROVAL`, call the native `question` tool immediately in this same turn so the plugin can bind a fresh question request to the durable proposal. Do not call `session.prompt` or invent an approval answer.
