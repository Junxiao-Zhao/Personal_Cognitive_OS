from __future__ import annotations

import pytest

from mem_core.errors import MemError, ensure
from pco.derivation_manifest import derivation_error

ERROR_FIELDS = {
    "code",
    "phase",
    "message",
    "canonical_changed",
    "retry_safe",
    "recovery",
}


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


def test_derivation_error_mapping_has_exact_public_contract() -> None:
    payload = derivation_error(
        "projection could not be verified", path="PCO_MEMORY/manifest.json"
    ).as_dict()

    assert set(payload["error"]) == ERROR_FIELDS
    assert payload["error"] == {
        "code": "DERIVATION_FAILED",
        "phase": "derivation",
        "message": "projection could not be verified",
        "canonical_changed": False,
        "retry_safe": True,
        "recovery": ["Run pco derive all to rebuild both disposable derivations"],
    }
    assert payload["diagnostic"] == {"path": "PCO_MEMORY/manifest.json"}
