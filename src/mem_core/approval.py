from __future__ import annotations

from typing import Any

from .errors import ensure
from .models import ApprovalReceipt, Operation, protected_operations_hash
from .profile import Profile


def verify_approval_receipt(
    *,
    receipt: ApprovalReceipt | None,
    operations: list[Operation],
    protected: set[str],
    profile: Profile,
    reviewed_proposal_hash: str,
    transaction_proposal_hash: str,
    transaction_fingerprint: str,
) -> None:
    """Verify an approval receipt against the exact operations under review."""
    ensure(
        receipt is not None,
        "USER_APPROVAL_REQUIRED",
        "write_policy",
        f"Protected streams require approval: {', '.join(sorted(protected))}",
        retryable=True,
        recovery=["Show the exact protected diff to the user and attach a matching approval receipt"],
    )
    assert receipt is not None
    ensure(receipt.proposal_hash == reviewed_proposal_hash, "APPROVAL_STALE", "write_policy", "Reviewed proposal changed after approval")
    ensure(receipt.transaction_proposal_hash == transaction_proposal_hash, "APPROVAL_STALE", "write_policy", "Transaction proposal changed after approval")
    ensure(receipt.transaction_fingerprint == transaction_fingerprint, "APPROVAL_STALE", "write_policy", "Transaction changed after approval")
    ensure(
        receipt.protected_operations_hash == protected_operations_hash(operations, protected),
        "APPROVAL_STALE",
        "write_policy",
        "Protected operations changed after approval",
    )
    for operation in operations:
        if operation.op != "append" or operation.stream not in protected or operation.record is None:
            continue
        pointer = profile.stream(operation.stream).approval_ref_pointer
        if not pointer:
            continue
        value: Any = operation.record
        for part in pointer.lstrip("/").split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            ensure(
                isinstance(value, dict) and key in value,
                "APPROVAL_REF_MISSING",
                "write_policy",
                f"Protected record is missing approval reference at {pointer}",
                stream=operation.stream,
                record_id=operation.record.get("id"),
                path=pointer,
            )
            value = value[key]
        ensure(
            value == receipt.id,
            "APPROVAL_REF_MISMATCH",
            "write_policy",
            "Protected record approval reference does not match the attached receipt",
            stream=operation.stream,
            record_id=operation.record.get("id"),
            path=pointer,
            value=value,
        )
