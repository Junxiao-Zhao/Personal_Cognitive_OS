from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from mem_core.models import ApplicationCommitRequest
from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.transaction import TransactionManager
from pco.paths import bundled_profile
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


def test_assistant_speaker_cannot_masquerade_as_user_authored_registered_evidence(
    minimal_records: dict[str, dict[str, Any]],
) -> None:
    """Ignoring speaker would allow an assistant summary to establish a personal fact."""
    records = _records(minimal_records)
    records["evidence"].append(
        _evidence(
            "ev_assistant_registered",
            speaker="assistant",
            source_kind="registered",
            user_authored=True,
        )
    )
    records["events"] = [
        _record_with_evidence(records, "events", "evidence:ev_assistant_registered")
    ]

    problem = _first_problem(records)

    assert problem["code"] == "EVIDENCE_INELIGIBLE"
    assert problem["path"] == "/payload/evidence_refs/0"


def test_assistant_registered_evidence_cannot_support_a_psychology_concept(
    minimal_records: dict[str, dict[str, Any]],
) -> None:
    """Allowing an assistant summary as concept evidence would violate the attribution boundary."""
    records = _records(minimal_records)
    records["evidence"].append(
        _evidence(
            "ev_assistant_concept",
            speaker="assistant",
            source_kind="registered",
            user_authored=True,
        )
    )
    records["psychologies"] = [
        _record_with_evidence(records, "psychologies", "evidence:ev_assistant_concept")
    ]

    problems = list(validate_profile(Path("."), records))

    assert [(problem["code"], problem["stream"], problem["path"]) for problem in problems] == [
        ("EVIDENCE_INELIGIBLE", "psychologies", "/payload/evidence_refs/0")
    ]


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


def test_source_full_transcript_requires_explicit_user_authorship(
    minimal_records: dict[str, dict[str, Any]],
) -> None:
    """A source cannot claim a full user transcript without provenance metadata."""
    records = _records(minimal_records)
    records["sources"][0]["payload"]["fidelity"] = "full_user_provided_transcript"

    problem = _first_problem(records)

    assert problem["code"] == "EVIDENCE_FIDELITY_INVALID"
    assert problem["stream"] == "sources"
    assert problem["path"] == "/payload/user_authored"


def test_source_schema_allows_explicit_user_authorship_for_full_transcript(
    minimal_records: dict[str, dict[str, Any]],
) -> None:
    """A closed source schema must carry the provenance needed by the fidelity rule."""
    record = deepcopy(minimal_records["sources"])
    record["payload"]["fidelity"] = "full_user_provided_transcript"
    record["payload"]["user_authored"] = True

    Profile.load(bundled_profile(), default_registry()).validate_record_schema("sources", record)


def test_historical_reference_remains_valid_after_current_records_retire(
    minimal_records: dict[str, dict[str, Any]],
) -> None:
    """Revalidating retired history against today’s current view would rewrite the past."""
    records = _records(minimal_records)
    old_event = deepcopy(records["events"][0])
    current_event = deepcopy(old_event)
    current_event["revision"] = 2
    current_event["payload"]["previous_revision"] = 1
    current_event["payload"]["evidence_refs"] = ["evidence:ev_current"]
    current_evidence = _evidence("ev_current")
    evidence_tombstone = deepcopy(records["evidence"][0])
    evidence_tombstone["revision"] = 2
    evidence_tombstone["payload"]["previous_revision"] = 1
    evidence_tombstone["payload"]["status"] = "tombstone"
    records["events"] = [old_event, current_event]
    records["evidence"] = [records["evidence"][0], evidence_tombstone, current_evidence]
    for stream in ("psychologies", "philosophies", "archetypes", "hypotheses", "meta_revisions"):
        records[stream][0]["payload"]["evidence_refs"] = ["evidence:ev_current"]

    assert list(validate_profile(Path("."), records)) == []


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


def test_duplicate_revision_ties_are_reported_deterministically(
    minimal_records: dict[str, dict[str, Any]],
) -> None:
    """Changing append order must not change duplicate-revision diagnostics."""
    records = _records(minimal_records)
    duplicate = deepcopy(records["evidence"][0])
    duplicate["payload"].update(
        {
            "change_summary": "Competing duplicate revision",
            "source_kind": "external",
            "user_authored": False,
        }
    )
    forward = {**records, "evidence": [records["evidence"][0], duplicate]}
    reverse = {**records, "evidence": [duplicate, records["evidence"][0]]}

    forward_problems = list(validate_profile(Path("."), forward))
    reverse_problems = list(validate_profile(Path("."), reverse))

    assert forward_problems == reverse_problems
    assert any(
        problem["code"] == "PROPOSAL_REVISION_CONFLICT"
        and problem["stream"] == "evidence"
        and problem["path"] == "/revision"
        for problem in forward_problems
    )


def test_counter_evidence_obeys_the_personal_fact_boundary(
    minimal_records: dict[str, dict[str, Any]],
) -> None:
    """Skipping counter-evidence would let external research establish a user fact."""
    records = _records(minimal_records)
    records["evidence"].append(
        _evidence("ev_external_counter", source_kind="external", user_authored=False)
    )
    records["hypotheses"][0]["payload"]["counter_evidence_refs"] = [
        "evidence:ev_external_counter"
    ]

    problem = _first_problem(records)

    assert problem["code"] == "EVIDENCE_INELIGIBLE"
    assert problem["path"] == "/payload/counter_evidence_refs/0"


def test_invalid_event_link_stream_is_a_structured_problem(
    minimal_records: dict[str, dict[str, Any]],
) -> None:
    """Silently ignoring a legacy or misspelled event-link stream loses a reference."""
    records = _records(minimal_records)
    records["events"][0]["payload"]["links"] = {"legacy_relations": ["rel_1"]}

    problem = _first_problem(records)

    assert problem["code"] == "PROPOSAL_REFERENCE_INVALID"
    assert problem["path"] == "/payload/links/legacy_relations"


def test_unknown_list_valued_stream_is_rejected_deterministically(
    minimal_records: dict[str, dict[str, Any]],
) -> None:
    """Treating an internal ledger as a business stream would revive v0.4 surfaces."""
    records = _records(minimal_records)
    records["transactions"] = []

    problem = _first_problem(records)

    assert problem["code"] == "STREAM_NOT_FOUND"
    assert problem["stream"] == "transactions"
    assert problem["path"] == "/transactions"


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
