---
description: Abort an uncommitted PCO v0.4.0 checkpoint
subtask: false
---

[PCO_CONTROL] Call `pco_abort` exactly once. It is valid only before canonical memory commit: clear the uncommitted checkpoint and unlock input without running native compact. After canonical commit, abort is forbidden—even if context publication, derivations, receipt insertion, or native compact failed; explain that committed memory cannot be aborted or rolled back and that `/pco-retry` is the recovery path.
