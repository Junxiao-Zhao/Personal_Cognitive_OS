from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest

from pco.checkpoint.authorization import consume, verify


def _grant(secret: str, **overrides: object) -> str:
    payload = {
        "grant_id": "grant_test",
        "checkpoint_id": "ckpt_test",
        "proposal_hash": "proposal_test",
        "challenge_id": "challenge_test",
        "session_id": "session_test",
        "issued_at": int(time.time()),
        **overrides,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def test_host_grant_is_bound_to_checkpoint_proposal_challenge_and_session() -> None:
    grant = _grant("secret")
    payload = verify(
        grant,
        secret="secret",
        checkpoint_id="ckpt_test",
        proposal_hash="proposal_test",
        challenge_id="challenge_test",
        session_id="session_test",
    )
    assert payload["grant_id"] == "grant_test"
    with pytest.raises(Exception):
        verify(
            grant,
            secret="secret",
            checkpoint_id="ckpt_other",
            proposal_hash="proposal_test",
            challenge_id="challenge_test",
            session_id="session_test",
        )


def test_expired_or_wrong_secret_grant_is_rejected() -> None:
    with pytest.raises(Exception):
        verify(
            _grant("secret", issued_at=int(time.time()) - 301),
            secret="secret",
            checkpoint_id="ckpt_test",
            proposal_hash="proposal_test",
            challenge_id="challenge_test",
            session_id="session_test",
        )
    with pytest.raises(Exception):
        verify(
            _grant("secret"),
            secret="wrong",
            checkpoint_id="ckpt_test",
            proposal_hash="proposal_test",
            challenge_id="challenge_test",
            session_id="session_test",
        )


def test_grant_requires_the_bound_session() -> None:
    with pytest.raises(Exception):
        verify(
            _grant("secret"),
            secret="secret",
            checkpoint_id="ckpt_test",
            proposal_hash="proposal_test",
            challenge_id="challenge_test",
            session_id=None,
        )


def test_grant_can_be_consumed_only_once(tmp_path: Path) -> None:
    payload = verify(
        _grant("secret"),
        secret="secret",
        checkpoint_id="ckpt_test",
        proposal_hash="proposal_test",
        challenge_id="challenge_test",
        session_id="session_test",
    )
    consume(payload, tmp_path)
    with pytest.raises(Exception):
        consume(payload, tmp_path)
