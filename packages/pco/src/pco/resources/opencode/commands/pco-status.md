---
description: Show the active PCO v0.4.0 checkpoint state
subtask: false
---

[PCO_CONTROL] Call `pco_status` exactly once and present the result without changing state. Preserve and report the durable `trigger` (`manual|auto`) and `intent` (`consolidate|compact`) without inferring either field. Include consolidation (`pending|no_op|committed`), context publication, derivations, compaction (`not_requested|pending|completed|failed`), receipt, cursor, and input-lock state. Make the result explicit: consolidate does not native-compact; compact is allowed to native-compact only after context publication succeeds. If the result is `AWAITING_META_APPROVAL`, call the native `question` tool immediately in this same turn so the plugin can bind a fresh question request to the durable proposal. Do not call `session.prompt` or invent an approval answer.
