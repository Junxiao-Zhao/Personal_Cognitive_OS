from __future__ import annotations

import shlex
import sys

import pytest

from mem_core.errors import MemError, ensure
from pco.projections import _project_affine_repository

ERROR_FIELDS = {
    "code",
    "phase",
    "message",
    "canonical_changed",
    "retry_safe",
    "recovery",
}


class EmptyProjectionRepository:
    root = None

    def records_by_stream(self) -> dict[str, list[dict]]:
        return {}

    def current_records(self, _stream: str) -> dict[str, dict]:
        return {}

    def head(self) -> str:
        return "commit_a"


def test_structured_error_reports_canonical_change_and_safe_retry() -> None:
    error = MemError(
        "PROPOSAL_STALE",
        "proposal_validation",
        "stale",
        canonical_changed=False,
        retry_safe=True,
        recovery=["Run pco proposal scaffold and re-author the proposal"],
    )

    assert error.as_dict()["error"] == {
        "code": "PROPOSAL_STALE",
        "phase": "proposal_validation",
        "message": "stale",
        "canonical_changed": False,
        "retry_safe": True,
        "recovery": ["Run pco proposal scaffold and re-author the proposal"],
    }


def test_structured_error_requires_canonical_retry_contract() -> None:
    with pytest.raises(TypeError):
        MemError("PROPOSAL_STALE", "proposal_validation", "stale")

    with pytest.raises(TypeError):
        ensure(False, "PROPOSAL_STALE", "proposal_validation", "stale")


def test_public_error_mapping_has_exact_six_fields_with_diagnostics_outside_error() -> (
    None
):
    error = MemError(
        "SCHEMA_VALIDATION_FAILED",
        "profile_validation",
        "record is invalid",
        stream="events",
        record_id="evt_1",
        path="/payload/status",
        value="paused",
        canonical_changed=False,
        retry_safe=True,
        recovery=["Correct the record at the reported JSON Pointer and validate again"],
    )

    payload = error.as_dict()

    assert set(payload["error"]) == ERROR_FIELDS
    assert payload["diagnostic"] == {
        "stream": "events",
        "record_id": "evt_1",
        "path": "/payload/status",
        "value": "paused",
    }


def test_projection_returned_error_mapping_has_exact_six_fields(tmp_path) -> None:
    result = _project_affine_repository(
        EmptyProjectionRepository(),
        tmp_path,
        None,
        "commit_a",
        {},
    )

    assert set(result["error"]) == ERROR_FIELDS
    assert result["error"] == {
        "code": "AFFINE_BRIDGE_NOT_CONFIGURED",
        "phase": "projection",
        "message": "AFFiNE bridge is not configured",
        "canonical_changed": False,
        "retry_safe": True,
        "recovery": [
            "Set PCO_AFFINE_COMMAND to an AFFiNE bridge CLI",
            "Use the Markdown projection target",
        ],
    }


def test_projection_returned_error_keeps_diagnostics_outside_error(tmp_path) -> None:
    script = "import sys; sys.stderr.write('bridge failed'); sys.exit(7)"
    result = _project_affine_repository(
        EmptyProjectionRepository(),
        tmp_path,
        f"{sys.executable} -c {shlex.quote(script)}",
        "commit_a",
        {},
    )

    assert set(result["error"]) == ERROR_FIELDS
    assert result["error"]["code"] == "AFFINE_SYNC_FAILED"
    assert result["diagnostic"] == {
        "diagnostic_path": str(tmp_path / "affine/logs/commit_a.log")
    }
