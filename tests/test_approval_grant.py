from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest

from mem_core.errors import MemError
from pco.checkpoint.authorization import consume, reason_hash, verify


def _grant(
    secret: str,
    *,
    decision: str = "yes",
    reason: str | None = None,
    **overrides: object,
) -> str:
    payload: dict[str, object] = {
        "grant_id": "grant_test",
        "checkpoint_id": "ckpt_test",
        "proposal_hash": "proposal_test",
        "approval_challenge_id": "challenge_test",
        "session_id": "session_test",
        "question_request_id": "question_test",
        "decision": decision,
        "reason_hash": None if decision == "yes" else reason_hash(reason or ""),
        "issued_at": int(time.time()),
        "expires_at": int(time.time()) + 300,
        **overrides,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _verify(grant: str, *, decision: str = "yes", reason: str | None = None, **overrides: object) -> dict[str, object]:
    return verify(
        grant,
        secret="secret",
        checkpoint_id=overrides.pop("checkpoint_id", "ckpt_test"),
        proposal_hash=overrides.pop("proposal_hash", "proposal_test"),
        approval_challenge_id=overrides.pop("approval_challenge_id", "challenge_test"),
        question_request_id=overrides.pop("question_request_id", "question_test"),
        decision=decision,  # type: ignore[arg-type]
        reason=reason,
        session_id=overrides.pop("session_id", "session_test"),
        **overrides,
    )


def test_host_grant_v2_binds_question_decision_and_all_durable_context() -> None:
    grant = _grant("secret")
    payload = _verify(grant)
    assert payload["grant_id"] == "grant_test"
    with pytest.raises(Exception):
        _verify(grant, question_request_id="question_other")
    with pytest.raises(Exception):
        _verify(grant, decision="no", reason="拒绝理由")


def test_no_grant_requires_exact_raw_reason_hash() -> None:
    raw_reason = "  用户补充的原始理由  "
    grant = _grant("secret", decision="no", reason=raw_reason)
    payload = _verify(grant, decision="no", reason=raw_reason)
    assert payload["reason_hash"] == reason_hash(raw_reason)
    with pytest.raises(Exception):
        _verify(grant, decision="no", reason=raw_reason.strip())


def test_expired_or_wrong_secret_grant_is_rejected() -> None:
    with pytest.raises(Exception):
        _verify(_grant("secret", expires_at=int(time.time()) - 1))
    with pytest.raises(Exception):
        verify(
            _grant("secret"),
            secret="wrong",
            checkpoint_id="ckpt_test",
            proposal_hash="proposal_test",
            approval_challenge_id="challenge_test",
            question_request_id="question_test",
            decision="yes",
            session_id="session_test",
        )


def test_malformed_grant_returns_structured_authorization_error() -> None:
    with pytest.raises(MemError) as exc_info:
        _verify("not-a-valid-grant")
    assert exc_info.value.detail.code == "APPROVAL_GRANT_INVALID"


def test_grant_requires_the_bound_session_and_v2_fields() -> None:
    with pytest.raises(Exception):
        _verify(_grant("secret"), session_id=None)
    with pytest.raises(Exception):
        _verify(_grant("secret", approval_challenge_id=None))


def test_grant_can_be_consumed_only_once(tmp_path: Path) -> None:
    payload = _verify(_grant("secret"))
    consume(payload, tmp_path)
    with pytest.raises(Exception):
        consume(payload, tmp_path)
