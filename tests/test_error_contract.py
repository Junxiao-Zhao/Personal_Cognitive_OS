from __future__ import annotations

import pytest

from mem_core.errors import MemError, ensure


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
