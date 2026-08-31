from __future__ import annotations

import json
from pathlib import Path

import pytest

from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository
from pco.paths import bundled_profile


NOW = "2026-08-30T10:00:00+00:00"


def make_public_logs_profile(root: Path) -> Path:
    schema_root = root / "schemas"
    schema_root.mkdir(parents=True)
    (schema_root / "log.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["id", "revision", "recorded_at", "schema_version", "payload"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "revision": {"type": "integer", "minimum": 1},
                    "recorded_at": {"type": "string", "format": "date-time"},
                    "schema_version": {"const": "public/log/v1"},
                    "payload": {
                        "type": "object",
                        "required": ["message"],
                        "properties": {"message": {"type": "string"}},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "profile.yaml").write_text(
        "\n".join(
            [
                "name: public-logs",
                "version: 1.0.0",
                "streams:",
                "  logs:",
                "    path: raw/logs.jsonl",
                "    schema: schemas/log.schema.json",
                "    schema_version: public/log/v1",
                "    write_policy: auto",
                "    validation:",
                "      transaction_mode: delta_only",
                "      run_cross_validators: false",
                "artifact_roots: []",
                "validators: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def repository(tmp_path: Path) -> MemoryRepository:
    profile = Profile.load(make_public_logs_profile(tmp_path / "profile"))
    result = MemoryRepository(tmp_path / "canonical", profile)
    result.init()
    return result


@pytest.fixture
def minimal_records() -> dict[str, dict]:
    """One hand-authored schema-valid create record for every v0.5 stream."""
    envelope = {
        "revision": 1,
        "recorded_at": NOW,
        "commit_ref": "proposal:receipt_1",
    }
    revision = {
        "previous_revision": None,
        "change_summary": "Create canonical record",
        "body_markdown": "A reviewed canonical statement.",
    }
    digest = "a" * 64
    return {
        "evidence": {
            **envelope,
            "id": "ev_1",
            "schema_version": "pco/evidence/v1",
            "payload": {
                **revision,
                "speaker": "user",
                "source_kind": "conversation",
                "user_authored": True,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
        },
        "sources": {
            **envelope,
            "id": "src_1",
            "schema_version": "pco/source/v1",
            "payload": {
                **revision,
                "locator": "file:///journal.md",
                "title": "Journal",
                "retrieved_at": NOW,
                "provider_label": "local file",
                "content_hash": f"sha256:{digest}",
                "snapshot_ref": f"source_blobs/{digest}",
                "fidelity": "registered_source_excerpt",
                "used_excerpts": ["A reviewed excerpt."],
                "status": "active",
            },
        },
        "events": {
            **envelope,
            "id": "evt_1",
            "schema_version": "pco/event/v1",
            "payload": {
                **revision,
                "occurred_at": {"precision": "day", "start": "2026-08-30"},
                "evidence_refs": ["evidence:ev_1"],
                "status": "active",
            },
        },
        "psychologies": {
            **envelope,
            "id": "psy_1",
            "schema_version": "pco/psychology/v1",
            "payload": {
                **revision,
                "name": "Reflection",
                "aliases": [],
                "evidence_refs": ["evidence:ev_1"],
                "status": "active",
            },
        },
        "philosophies": {
            **envelope,
            "id": "phi_1",
            "schema_version": "pco/philosophy/v1",
            "payload": {
                **revision,
                "name": "Pragmatism",
                "aliases": [],
                "evidence_refs": ["evidence:ev_1"],
                "status": "active",
            },
        },
        "archetypes": {
            **envelope,
            "id": "arc_1",
            "schema_version": "pco/archetype/v1",
            "payload": {
                **revision,
                "name": "The Builder",
                "aliases": [],
                "stance": "identify",
                "evidence_refs": ["evidence:ev_1"],
                "status": "active",
            },
        },
        "hypotheses": {
            **envelope,
            "id": "hyp_1",
            "schema_version": "pco/hypothesis/v1",
            "payload": {
                **revision,
                "confidence": 0.45,
                "evidence_refs": ["evidence:ev_1"],
                "counter_evidence_refs": [],
                "status": "hypothesis",
            },
        },
        "meta_revisions": {
            **envelope,
            "id": "meta_current",
            "schema_version": "pco/meta-revision/v1",
            "payload": {
                **revision,
                "evidence_refs": ["evidence:ev_1"],
                "promotion_refs": [],
                "status": "active",
            },
        },
        "session_handoffs": {
            **envelope,
            "id": "handoff_1",
            "schema_version": "pco/session-handoff/v1",
            "payload": {
                **revision,
                "topics": ["Canonical profile migration"],
                "unanswered_questions": [],
                "active_tensions": [],
                "recent_decisions": [],
                "next_directions": ["Validate the new contract"],
                "status": "active",
            },
        },
    }


@pytest.fixture
def v05_repository(tmp_path: Path) -> MemoryRepository:
    profile = Profile.load(bundled_profile(), default_registry())
    result = MemoryRepository(tmp_path / "canonical", profile)
    result.init()
    return result
