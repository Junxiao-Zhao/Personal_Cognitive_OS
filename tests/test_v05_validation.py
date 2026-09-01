from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from mem_core.models import ApplicationCommitRequest
from mem_core.transaction import TransactionManager
from pco.validation import validate_profile


def _records(minimal_records: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    records = {stream: [deepcopy(record)] for stream, record in minimal_records.items()}
    for stream_records in records.values():
        for record in stream_records:
            record["commit_ref"] = "commit_fixture"
    return records


def _evidence(record_id: str, **payload_updates: Any) -> dict[str, Any]:
    payload = {
        "previous_revision": None,
        "change_summary": "Capture evidence",
        "body_markdown": "A reviewed evidence excerpt.",
        "speaker": "user",
        "source_kind": "conversation",
        "user_authored": True,
        "fidelity": "selected_excerpt",
        "status": "active",
        **payload_updates,
    }
    return {
        "id": record_id,
        "revision": 1,
        "recorded_at": "2026-08-30T10:00:00+00:00",
        "schema_version": "pco/evidence/v1",
        "commit_ref": "commit_fixture",
        "payload": payload,
    }


def _record_with_evidence(
    records: dict[str, list[dict[str, Any]]], stream: str, evidence_ref: str
) -> dict[str, Any]:
    record = deepcopy(records[stream][0])
    record["id"] = f"{stream}_policy"
    record["payload"]["evidence_refs"] = [evidence_ref]
    return record


def _first_problem(records: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return next(iter(validate_profile(Path("."), records)))


def test_assistant_summary_cannot_support_personal_fact(minimal_records: dict[str, dict[str, Any]]) -> None:
    """Changing the personal-evidence policy to accept assistant output must fail."""
    records = _records(minimal_records)
    records["evidence"].append(
        _evidence("ev_assistant", speaker="assistant", user_authored=False)
    )
    records["events"] = [_record_with_evidence(records, "events", "evidence:ev_assistant")]

    problem = _first_problem(records)

    assert problem["code"] == "EVIDENCE_INELIGIBLE"
    assert problem["stream"] == "events"
    assert problem["path"] == "/payload/evidence_refs/0"


@pytest.mark.parametrize("stream", ["events", "archetypes", "hypotheses", "meta_revisions"])
def test_external_evidence_cannot_support_personal_records(
    minimal_records: dict[str, dict[str, Any]], stream: str
) -> None:
    """Accepting external material as a personal fact source would violate attribution."""
    records = _records(minimal_records)
    records["evidence"].append(
        _evidence("ev_external", source_kind="external", user_authored=False)
    )
    records[stream] = [_record_with_evidence(records, stream, "evidence:ev_external")]

    assert _first_problem(records)["code"] == "EVIDENCE_INELIGIBLE"


@pytest.mark.parametrize("stream", ["psychologies", "philosophies"])
def test_external_evidence_supports_concepts(
    minimal_records: dict[str, dict[str, Any]], stream: str
) -> None:
    """Rejecting external research for concepts would remove its intended use."""
    records = _records(minimal_records)
    records["evidence"].append(
        _evidence("ev_external", source_kind="external", user_authored=False)
    )
    records[stream] = [_record_with_evidence(records, stream, "evidence:ev_external")]

    assert list(validate_profile(Path("."), records)) == []


@pytest.mark.parametrize("source_kind", ["conversation", "registered"])
def test_user_authored_material_supports_personal_events(
    minimal_records: dict[str, dict[str, Any]], source_kind: str
) -> None:
    """Rejecting explicitly user-authored material would discard legitimate personal evidence."""
    records = _records(minimal_records)
    records["evidence"].append(_evidence("ev_user", source_kind=source_kind))
    records["events"] = [_record_with_evidence(records, "events", "evidence:ev_user")]

    assert list(validate_profile(Path("."), records)) == []


def test_registered_material_without_user_authorship_cannot_support_personal_fact(
    minimal_records: dict[str, dict[str, Any]],
) -> None:
    """Dropping the explicit-user-authorship requirement would admit unsupported facts."""
    records = _records(minimal_records)
    records["evidence"].append(
        _evidence("ev_registered", source_kind="registered", user_authored=False)
    )
    records["events"] = [_record_with_evidence(records, "events", "evidence:ev_registered")]

    assert _first_problem(records)["code"] == "EVIDENCE_INELIGIBLE"


def test_retired_target_is_not_a_current_reference(minimal_records: dict[str, dict[str, Any]]) -> None:
    """Treating a tombstoned hypothesis as current would revive retired memory."""
    records = _records(minimal_records)
    tombstone = deepcopy(records["hypotheses"][0])
    tombstone["revision"] = 2
    tombstone["payload"]["previous_revision"] = 1
    tombstone["payload"]["status"] = "tombstone"
    records["hypotheses"].append(tombstone)
    records["meta_revisions"][0]["payload"]["promotion_refs"] = ["hypothesis:hyp_1"]

    problem = _first_problem(records)

    assert problem["code"] == "PROPOSAL_REFERENCE_INVALID"
    assert problem["path"] == "/payload/promotion_refs/0"


def test_invalid_fidelity_returns_a_structured_problem(minimal_records: dict[str, dict[str, Any]]) -> None:
    """Accepting a fourth fidelity value would weaken the evidence contract."""
    records = _records(minimal_records)
    records["evidence"][0]["payload"]["fidelity"] = "summary"

    problem = _first_problem(records)

    assert problem["code"] == "EVIDENCE_FIDELITY_INVALID"
    assert problem["stream"] == "evidence"
    assert problem["path"] == "/payload/fidelity"
    assert problem["retryable"] is True
    assert problem["recovery"]


def test_source_fidelity_uses_the_same_closed_v05_vocabulary(
    minimal_records: dict[str, dict[str, Any]],
) -> None:
    """Allowing an unapproved source fidelity would make provenance incomparable."""
    records = _records(minimal_records)
    records["sources"][0]["payload"]["fidelity"] = "full_source"

    problem = _first_problem(records)

    assert problem["code"] == "EVIDENCE_FIDELITY_INVALID"
    assert problem["stream"] == "sources"
    assert problem["path"] == "/payload/fidelity"


def test_revision_chain_requires_the_immediately_previous_revision(
    minimal_records: dict[str, dict[str, Any]],
) -> None:
    """Allowing a revision to skip history would break append-only continuity."""
    records = _records(minimal_records)
    revision = deepcopy(records["events"][0])
    revision["revision"] = 3
    revision["payload"]["previous_revision"] = 2
    records["events"].append(revision)

    problem = _first_problem(records)

    assert problem["code"] == "PROPOSAL_REVISION_CONFLICT"
    assert problem["path"] == "/revision"


def test_commit_ref_must_identify_a_proposal_receipt(minimal_records: dict[str, dict[str, Any]]) -> None:
    """Accepting non-receipt commit references would sever the canonical audit link."""
    records = _records(minimal_records)
    records["events"][0]["commit_ref"] = "proposal:receipt_1"

    problem = _first_problem(records)

    assert problem["code"] == "PROPOSAL_REFERENCE_INVALID"
    assert problem["path"] == "/commit_ref"


def test_new_event_and_new_evidence_validate_in_one_delta(
    v05_repository, minimal_records: dict[str, dict[str, Any]]
) -> None:
    """Validating only the base would reject valid mutually dependent appends."""
    receipt_id = "commit_delta"
    evidence = _evidence("ev_delta")
    evidence["commit_ref"] = receipt_id
    event = deepcopy(minimal_records["events"])
    event["id"] = "evt_delta"
    event["commit_ref"] = receipt_id
    event["payload"]["evidence_refs"] = ["evidence:ev_delta"]
    operations = [
        {
            "op": "append",
            "stream": "evidence",
            "record": evidence,
        },
        {
            "op": "append",
            "stream": "events",
            "record": event,
        },
    ]
    request = ApplicationCommitRequest(
        policy="pco_proposal",
        transaction_digest="sha256:" + "d" * 64,
        base_commit=v05_repository.head(),
        operations=operations,
        receipt={
            "type": "proposal_commit_receipt",
            "receipt_id": receipt_id,
            "review_id": "review_delta",
            "committed_at": "2026-08-31T10:00:00+00:00",
        },
    )

    result = TransactionManager(
        v05_repository, v05_repository.root.parent / "tmp"
    ).commit_application(request)

    assert result["ok"] is True
