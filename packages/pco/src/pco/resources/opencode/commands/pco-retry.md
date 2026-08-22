---
description: Retry PCO v0.4.0 checkpoint recovery or pending post-commit work
subtask: false
---

[PCO_CONTROL] Call `pco_retry` exactly once and show the result. Resume the same durable checkpoint with its original `trigger` and `intent`; never downgrade or recompute them. Route recovery from the persisted failure phase: archive/freeze/worker/validation resumes consolidate, publication retries publication, derivation retries only pending derivations, native compact failure retries only native compact, and receipt failure retries receipt/unlock. Do not repeat a successful canonical commit, context publication, or native compact. If it returns `AWAITING_META_APPROVAL`, call the native `question` tool immediately in this same turn so the plugin can bind a fresh question request to the durable proposal.
