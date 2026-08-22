---
description: Run a PCO memory checkpoint and publish context without compacting the Harness conversation
subtask: false
---

[PCO_CONTROL] This is the `consolidate` intent. Call `pco_checkpoint` exactly once with no arguments; the Host-bound command provenance supplies `trigger=manual` and `intent=consolidate`. Never pass trigger or intent fields to the tool.

If it commits without approval, show the receipt and explain: `/consolidate` updates memory, but does not compact the conversation context. If approval is required:

1. Show the exact protected Meta diff, main evidence, and proposal hash.
2. Ask the native `question` tool for the fixed approval option or a non-empty custom/Other reason. The host question lifecycle, not ordinary question text or a model-generated tool call, is the authorization boundary.
3. Call `pco_approve` or `pco_reject` exactly once only after the host has produced the matching decision grant. Preserve a rejection answer exactly; do not approve or reject based on an untracked answer or a bare `No`.
4. Show the final checkpoint receipt and state that the conversation context was not compacted.

Do not call any native compact command yourself.
