"""Host-issued, short-lived decision grants for protected checkpoints."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

from mem_core.errors import MemError, ensure


Decision = Literal["yes", "no"]


def reason_hash(reason: str) -> str:
    """Hash the exact UTF-8 bytes supplied by the question answer."""

    return "sha256:" + hashlib.sha256(reason.encode("utf-8")).hexdigest()


def _decode(value: str) -> Any:
    padding = "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode((value + padding).encode("ascii")))


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    ensure(isinstance(value, str) and bool(value), "APPROVAL_GRANT_INVALID", "approval", f"Approval grant has no valid {field}")
    return value


def verify(
    grant: str | None,
    *,
    secret: str | None,
    checkpoint_id: str,
    proposal_hash: str,
    approval_challenge_id: str | None = None,
    question_request_id: str | None,
    decision: Decision,
    reason: str | None = None,
    session_id: str | None,
    max_age_seconds: int = 300,
    # Kept as a call-site compatibility alias; v2 payloads still require
    # approval_challenge_id and never fall back to the v1 payload shape.
    challenge_id: str | None = None,
) -> dict[str, Any]:
    ensure(grant, "APPROVAL_GRANT_REQUIRED", "approval", "Decision must come from the host-issued question interaction")
    ensure(secret, "APPROVAL_GRANT_UNAVAILABLE", "approval", "The host approval verifier is not configured")
    if approval_challenge_id is None:
        approval_challenge_id = challenge_id
    ensure(isinstance(approval_challenge_id, str) and bool(approval_challenge_id), "APPROVAL_CHALLENGE_REQUIRED", "approval", "Approval grant verification requires the bound challenge")
    ensure(isinstance(question_request_id, str) and bool(question_request_id), "QUESTION_REQUEST_REQUIRED", "approval", "Approval grant verification requires the native question request")
    ensure(decision in {"yes", "no"}, "APPROVAL_DECISION_INVALID", "approval", "Approval decision must be yes or no")
    ensure(isinstance(session_id, str) and bool(session_id.strip()), "APPROVAL_SESSION_REQUIRED", "approval", "Approval grant verification requires the bound harness session")
    try:
        encoded, signature = str(grant).split(".", 1)
        expected = hmac.new(str(secret).encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        payload = _decode(encoded)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise MemError(
            "APPROVAL_GRANT_INVALID",
            "approval",
            "Malformed approval grant",
            retryable=False,
            recovery=["Complete the native question again to obtain a fresh approval grant"],
        ) from exc
    ensure(hmac.compare_digest(signature, expected), "APPROVAL_GRANT_INVALID", "approval", "Approval grant signature is invalid")
    ensure(isinstance(payload, dict), "APPROVAL_GRANT_INVALID", "approval", "Approval grant payload is invalid")

    grant_id = _required_text(payload, "grant_id")
    ensure(payload.get("checkpoint_id") == checkpoint_id, "APPROVAL_GRANT_STALE", "approval", "Approval grant targets a different checkpoint")
    ensure(payload.get("proposal_hash") == proposal_hash, "APPROVAL_GRANT_STALE", "approval", "Approval grant targets a different proposal")
    ensure(payload.get("approval_challenge_id") == approval_challenge_id, "APPROVAL_GRANT_STALE", "approval", "Approval grant targets a different approval challenge")
    ensure(payload.get("session_id") == session_id, "APPROVAL_GRANT_STALE", "approval", "Approval grant targets a different harness session")
    ensure(payload.get("question_request_id") == question_request_id, "APPROVAL_GRANT_STALE", "approval", "Approval grant targets a different native question")
    ensure(payload.get("decision") == decision, "APPROVAL_GRANT_DECISION_MISMATCH", "approval", "Approval grant decision does not match the requested operation")

    issued_at = payload.get("issued_at")
    expires_at = payload.get("expires_at")
    ensure(isinstance(issued_at, (int, float)) and not isinstance(issued_at, bool), "APPROVAL_GRANT_INVALID", "approval", "Approval grant has no valid issue time")
    ensure(isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool), "APPROVAL_GRANT_INVALID", "approval", "Approval grant has no valid expiry time")
    now = time.time()
    ensure(float(expires_at) > float(issued_at), "APPROVAL_GRANT_INVALID", "approval", "Approval grant expiry is not after issue time")
    ensure(float(expires_at) - float(issued_at) <= max_age_seconds, "APPROVAL_GRANT_INVALID", "approval", "Approval grant TTL exceeds the host policy")
    ensure(now <= float(expires_at), "APPROVAL_GRANT_EXPIRED", "approval", "Approval grant has expired")
    ensure(now - float(issued_at) >= -30, "APPROVAL_GRANT_INVALID", "approval", "Approval grant issue time is in the future")

    ensure("reason_hash" in payload, "APPROVAL_GRANT_INVALID", "approval", "Approval grant is missing the v2 reason_hash field")
    supplied_reason_hash = payload["reason_hash"]
    if decision == "yes":
        ensure(supplied_reason_hash is None, "APPROVAL_GRANT_INVALID", "approval", "Yes grants cannot carry a rejection reason")
        ensure(reason is None, "APPROVAL_REASON_UNEXPECTED", "approval", "Yes cannot include a rejection reason")
    else:
        ensure(isinstance(reason, str) and bool(reason.strip()), "REJECTION_REASON_REQUIRED", "approval", "No requires a reason or supplemental experience", path="/reason")
        ensure(isinstance(supplied_reason_hash, str) and supplied_reason_hash == reason_hash(reason), "APPROVAL_REASON_MISMATCH", "approval", "The rejection reason does not match the host-issued grant")

    return {**payload, "grant_id": grant_id}


def consume(payload: dict[str, Any], state_root: Path) -> None:
    """Atomically mark a verified grant as consumed."""

    grant_id = payload.get("grant_id")
    ensure(isinstance(grant_id, str) and bool(grant_id), "APPROVAL_GRANT_INVALID", "approval", "Approval grant has no valid grant ID")
    marker_dir = Path(state_root) / "approval-grants"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_name = hashlib.sha256(grant_id.encode("utf-8")).hexdigest() + ".json"
    marker_path = marker_dir / marker_name
    try:
        fd = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        ensure(False, "APPROVAL_GRANT_REPLAYED", "approval", "Approval grant has already been consumed")
        raise AssertionError("unreachable") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({**payload, "consumed_at": time.time()}, handle, ensure_ascii=False, sort_keys=True)
