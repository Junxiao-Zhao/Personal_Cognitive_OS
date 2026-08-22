---
description: Consolidate PCO memory, publish approved context, then compact the Harness conversation
subtask: false
---

[PCO_CONTROL] This is the `compact` intent. Call `pco_checkpoint` exactly once with no arguments; the Host-bound command provenance supplies `trigger=manual` and `intent=compact`. Never pass trigger or intent fields to the tool. If it commits without approval, show its receipt. If approval is required:

1. Show the exact protected Meta diff, main evidence, and proposal hash.
2. Show the user the exact hash and ask the native `question` tool for the fixed approval option or a non-empty custom/Other reason. The host question lifecycle, not ordinary question text or a model-generated tool call, is the authorization boundary.
3. Call `pco_approve` or `pco_reject` exactly once only after the host has produced the matching decision grant. Preserve the rejection answer exactly; do not approve or reject based on an untracked question answer or a bare `No`.
4. Show the final checkpoint receipt. After a rejection, do not ask another follow-up.

Do not call any native compact command yourself. PCO will invoke it once, only after canonical memory commit and context publication succeed.
