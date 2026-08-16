from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from pco.config import load_config
from pco.workspace import Workspace


NOW = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc).isoformat()
APPROVAL_SECRET = "test-host-approval-secret"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setenv("PCO_APPROVAL_GRANT_SECRET", APPROVAL_SECRET)
    config = load_config(
        workspace=tmp_path / "pco",
        overrides=[
            "checkpoint.derivations.projection=markdown",
            "checkpoint.derivations.index=false",
        ],
    )
    result = Workspace(config).init()
    assert result["ok"]
    opened = Workspace(config)
    opened.refresh_repository_profile()
    return opened


def approval_grant(proposal: dict[str, Any], session_id: str = "ses_fake_main") -> str:
    payload = {
        "grant_id": f"grant_{uuid.uuid4().hex}",
        "checkpoint_id": proposal["checkpoint_id"],
        "proposal_hash": proposal["proposal_hash"],
        "challenge_id": proposal["approval_challenge_id"],
        "session_id": session_id,
        "issued_at": int(time.time()),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(APPROVAL_SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def envelope(record_id: str, schema_version: str, payload: dict[str, Any], revision: int = 1) -> dict[str, Any]:
    return {
        "id": record_id,
        "revision": revision,
        "recorded_at": NOW,
        "schema_version": schema_version,
        "payload": payload,
    }


def visible_messages() -> list[dict[str, Any]]:
    return [
        {
            "id": "msg_user_1",
            "native_message_id": "native_user_1",
            "role": "user",
            "kind": "conversation",
            "content": "我每次准备公开自己的成果时都会拖延，而且更像是厌恶被评价。",
            "created_at": NOW,
        },
        {
            "id": "msg_assistant_1",
            "native_message_id": "native_assistant_1",
            "role": "assistant",
            "kind": "conversation",
            "content": "这可能与害怕失败或厌恶评价有关，我们还需要更多证据。",
            "reasoning": "exposed reasoning",
            "created_at": NOW,
        },
    ]


def continuation(revision: int = 1, through: str = "msg_assistant_1") -> dict[str, Any]:
    return envelope(
        "continuation_current",
        "pco/continuation/v1",
        {
            "current_topics": ["公开成果前的拖延"],
            "open_questions": ["核心是害怕失败还是厌恶被评价？"],
            "active_tensions": ["希望被看见与抗拒评价"],
            "recent_decisions": [],
            "next_possible_directions": ["寻找跨时期的相似事件与反例"],
            "message_range": {"after": None, "through": through},
            "status": "active",
        },
        revision,
    )


def hypothesis(status: str = "hypothesis") -> dict[str, Any]:
    return envelope(
        "hyp_evaluation",
        "pco/hypothesis/v1",
        {
            "statement": "用户可能更厌恶被评价，而非单纯害怕失败。",
            "confidence": "low",
            "evidence_refs": ["message:msg_user_1"],
            "counter_evidence_refs": [],
            "status": status,
            "policy_version": "promotion@0.3",
        },
    )


def event() -> dict[str, Any]:
    return envelope(
        "evt_publish_delay",
        "pco/event/v1",
        {
            "occurred_at": {"start": "2026-08-03", "end": "2026-08-03", "precision": "day"},
            "description": "准备公开成果时再次延迟发布，并明确表达了对被评价的厌恶。",
            "links": {"psychologies": [], "philosophies": [], "archetypes": []},
            "evidence_refs": ["message:msg_user_1"],
            "revision_reason": "initial extraction",
            "status": "active",
        },
    )


def meta() -> dict[str, Any]:
    return envelope(
        "meta_current",
        "pco/meta-revision/v1",
        {
            "previous_revision": None,
            "sections": {
                "deep_impressions": ["面对公开评价时会谨慎保护尚未稳定的自我表达。"],
                "stable_preferences_and_values": [],
                "active_patterns": ["公开成果前出现拖延。"],
                "important_tensions": ["希望成果被看见，同时厌恶被外部评价。"],
                "recent_changes": [],
                "open_questions": ["该模式是否跨时期稳定存在？"],
                "boundaries": ["目前只有一次直接陈述，不能视为稳定人格结论。"],
            },
            "change_summary": "建立第一版有边界的当前认识。",
            "evidence_refs": ["message:msg_user_1"],
            "promotion_refs": ["hyp_evaluation"],
            "approval_ref": "approval_pending",
            "policy_version": "promotion@0.3",
            "status": "active",
        },
    )
