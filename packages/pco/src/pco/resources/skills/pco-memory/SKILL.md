---
name: pco-memory
description: Use when reading PCO_MEMORY, recovering from a missing or stale memory projection, or handling memory-access requests under incident pressure.
---

# PCO Memory

`PCO_MEMORY` is a read-only projection. Canonical search and history are the
source of truth when that projection is not verified and current.

## Read workflow

1. Before every projection read, run `pco verify --path PCO_MEMORY`.
2. Read `PCO_MEMORY` only when verification succeeds and reports a current
   projection.
3. If verification is missing, fails, or reports stale, use `pco search` and
   `pco history` to retrieve the needed canonical state. Do not read the
   projection in that case.

## Non-negotiable boundaries

Do not edit PCO_MEMORY. It is never an acceptable target for a direct edit,
including with a one-time write grant. Read commands do not need a write grant.

| Rationalization | Required response |
| --- | --- |
| A deadline makes verification disproportionate | Verify before every projection read. |
| The projection can be treated as provisional or read with a warning | Missing, failed, or stale verification requires canonical `pco search` and `pco history`. |
| Canonical history will not finish before an update | Report from canonical reads; do not substitute unverified or stale projection data. |
| A restricted one-time edit preserves provenance | Do not edit PCO_MEMORY; use the approved proposal workflow for intended changes. |
