"""Host-issued, short-lived approval grants for protected checkpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

from mem_core.errors import ensure


def _decode(value: str) -> Any:
    padding = "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode((value + padding).encode("ascii")))


def verify(
    grant: str | None,
    *,
    secret: str | None,
    checkpoint_id: str,
    proposal_hash: str,
    challenge_id: str | None,
    session_id: str | None,
    max_age_seconds: int = 300,
) -> dict[str, Any]:
    ensure(grant, "APPROVAL_GRANT_REQUIRED", "approval", "Approval must come from the host-issued approval interaction")
    ensure(secret, "APPROVAL_GRANT_UNAVAILABLE", "approval", "The host approval verifier is not configured")
    try:
        encoded, signature = str(grant).split(".", 1)
        expected = hmac.new(str(secret).encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        payload = _decode(encoded)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Malformed approval grant") from exc
    ensure(hmac.compare_digest(signature, expected), "APPROVAL_GRANT_INVALID", "approval", "Approval grant signature is invalid")
    ensure(isinstance(payload, dict), "APPROVAL_GRANT_INVALID", "approval", "Approval grant payload is invalid")
    ensure(isinstance(payload.get("grant_id"), str) and bool(payload["grant_id"]), "APPROVAL_GRANT_INVALID", "approval", "Approval grant has no valid grant ID")
    ensure(payload.get("checkpoint_id") == checkpoint_id, "APPROVAL_GRANT_STALE", "approval", "Approval grant targets a different checkpoint")
    ensure(payload.get("proposal_hash") == proposal_hash, "APPROVAL_GRANT_STALE", "approval", "Approval grant targets a different proposal")
    ensure(payload.get("challenge_id") == challenge_id, "APPROVAL_GRANT_STALE", "approval", "Approval grant targets a different approval challenge")
    ensure(isinstance(session_id, str) and bool(session_id.strip()), "APPROVAL_SESSION_REQUIRED", "approval", "Approval grant verification requires the bound harness session")
    ensure(payload.get("session_id") == session_id, "APPROVAL_GRANT_STALE", "approval", "Approval grant targets a different harness session")
    issued_at = payload.get("issued_at")
    ensure(isinstance(issued_at, (int, float)), "APPROVAL_GRANT_INVALID", "approval", "Approval grant has no valid issue time")
    ensure(time.time() - float(issued_at) <= max_age_seconds, "APPROVAL_GRANT_EXPIRED", "approval", "Approval grant has expired")
    ensure(time.time() - float(issued_at) >= -30, "APPROVAL_GRANT_INVALID", "approval", "Approval grant issue time is in the future")
    return payload


def consume(payload: dict[str, Any], state_root: Path) -> None:
    """Atomically mark a verified grant as consumed.

    The marker is deliberately created before the protected transaction starts.
    A failed commit therefore requires a newly minted grant instead of allowing
    the same host approval token to be replayed.
    """

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
