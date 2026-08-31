from __future__ import annotations

import json
from pathlib import Path

import pytest

from mem_core.profile import Profile
from mem_core.repository import MemoryRepository


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
