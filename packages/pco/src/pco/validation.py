from __future__ import annotations

import re
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

APPROVED_FIDELITIES = frozenset(
    {"selected_excerpt", "full_user_provided_transcript", "registered_source_excerpt"}
)
CANONICAL_BUSINESS_STREAMS = frozenset(
    {
        "evidence",
        "sources",
        "events",
        "psychologies",
        "philosophies",
        "archetypes",
        "hypotheses",
        "meta_revisions",
        "session_handoffs",
    }
)
PERSONAL_FACT_STREAMS = frozenset(
    {"events", "archetypes", "hypotheses", "meta_revisions"}
)
CONCEPT_STREAMS = frozenset({"psychologies", "philosophies"})
EVIDENCE_FIELDS: Mapping[str, tuple[str, ...]] = {
    "events": ("evidence_refs",),
    "psychologies": ("evidence_refs",),
    "philosophies": ("evidence_refs",),
    "archetypes": ("evidence_refs",),
    "hypotheses": ("evidence_refs", "counter_evidence_refs"),
    "meta_revisions": ("evidence_refs",),
}
EVENT_LINK_STREAMS = frozenset({"psychologies", "philosophies", "archetypes"})
COMMIT_REF_PATTERN = re.compile(r"^commit_[A-Za-z0-9][A-Za-z0-9._-]*$")


def _problem(
    code: str,
    message: str,
    *,
    stream: str,
    record_id: Any,
    path: str,
    value: Any = None,
    recovery: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "phase": "profile_validation",
        "message": message,
        "stream": stream,
        "record_id": record_id,
        "path": path,
        "value": value,
        "canonical_changed": False,
        "retry_safe": True,
        "recovery": recovery
        or ["Correct the record at the reported JSON Pointer and validate again"],
    }


def _payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("payload")
    return value if isinstance(value, Mapping) else {}


def _record_key(record: Mapping[str, Any]) -> tuple[str, int, str]:
    revision = record.get("revision")
    return (
        str(record.get("id")),
        (
            revision
            if isinstance(revision, int) and not isinstance(revision, bool)
            else -1
        ),
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
    )


def _history_by_revision(
    items: Iterable[Mapping[str, Any]],
) -> dict[str, dict[int, list[Mapping[str, Any]]]]:
    history: dict[str, dict[int, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for record in sorted(items, key=_record_key):
        record_id, revision = record.get("id"), record.get("revision")
        if (
            isinstance(record_id, str)
            and isinstance(revision, int)
            and not isinstance(revision, bool)
        ):
            history[record_id][revision].append(record)
    return history


def latest_effective(
    items: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Return latest current records, excluding IDs retired by a tombstone."""
    latest: dict[str, Mapping[str, Any]] = {}
    for record in sorted(items, key=_record_key):
        record_id, revision = record.get("id"), record.get("revision")
        if (
            not isinstance(record_id, str)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
        ):
            continue
        previous = latest.get(record_id)
        if previous is None or revision > previous.get("revision", -1):
            latest[record_id] = record
    return {
        record_id: record
        for record_id, record in latest.items()
        if _payload(record).get("status") != "tombstone"
    }


def validate_revision_chain(
    stream: str,
    record: Mapping[str, Any],
    history: Mapping[str, Mapping[int, list[Mapping[str, Any]]]],
) -> Iterable[dict[str, Any]]:
    record_id, revision, payload = (
        record.get("id"),
        record.get("revision"),
        _payload(record),
    )
    if (
        not isinstance(record_id, str)
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
    ):
        yield _problem(
            "PROPOSAL_REVISION_CONFLICT",
            "A canonical revision must have a positive integer revision number",
            stream=stream,
            record_id=record_id,
            path="/revision",
            value=revision,
        )
        return
    revisions = history.get(record_id, {})
    if len(revisions.get(revision, [])) != 1:
        yield _problem(
            "PROPOSAL_REVISION_CONFLICT",
            "A record ID may have exactly one entry for each revision",
            stream=stream,
            record_id=record_id,
            path="/revision",
            value=revision,
            recovery=[
                "Append one complete next revision; do not duplicate a historical revision"
            ],
        )
    expected_previous = None if revision == 1 else revision - 1
    if payload.get("previous_revision") != expected_previous:
        yield _problem(
            "PROPOSAL_REVISION_CONFLICT",
            f"Revision {revision} must declare previous_revision {expected_previous!r}",
            stream=stream,
            record_id=record_id,
            path="/payload/previous_revision",
            value=payload.get("previous_revision"),
            recovery=[
                "Set previous_revision to the immediately preceding canonical revision"
            ],
        )
    if revision > 1 and len(revisions.get(revision - 1, [])) != 1:
        yield _problem(
            "PROPOSAL_REVISION_CONFLICT",
            "A revision may only follow an existing immediately preceding revision",
            stream=stream,
            record_id=record_id,
            path="/revision",
            value=revision,
            recovery=["Append the missing preceding revision before this record"],
        )


def validate_commit_ref(
    stream: str, record: Mapping[str, Any]
) -> Iterable[dict[str, Any]]:
    commit_ref = record.get("commit_ref")
    if (
        not isinstance(commit_ref, str)
        or COMMIT_REF_PATTERN.fullmatch(commit_ref) is None
    ):
        yield _problem(
            "PROPOSAL_REFERENCE_INVALID",
            "commit_ref must identify a proposal commit receipt",
            stream=stream,
            record_id=record.get("id"),
            path="/commit_ref",
            value=commit_ref,
            recovery=[
                "Use the generated commit_<receipt> reference from the proposal receipt"
            ],
        )


def _reference_problem(
    stream: str, record: Mapping[str, Any], path: str, value: Any, message: str
) -> dict[str, Any]:
    return _problem(
        "PROPOSAL_REFERENCE_INVALID",
        message,
        stream=stream,
        record_id=record.get("id"),
        path=path,
        value=value,
        recovery=["Reference an existing current record or remove the reference"],
    )


def validate_references(
    stream: str,
    record: Mapping[str, Any],
    current: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> Iterable[dict[str, Any]]:
    payload = _payload(record)
    for field in EVIDENCE_FIELDS.get(stream, ()):
        refs = payload.get(field, [])
        if not isinstance(refs, list):
            continue
        for index, reference in enumerate(refs):
            prefix, separator, evidence_id = (
                reference.partition(":") if isinstance(reference, str) else ("", "", "")
            )
            if (
                prefix != "evidence"
                or not separator
                or evidence_id not in current.get("evidence", {})
            ):
                yield _reference_problem(
                    stream,
                    record,
                    f"/payload/{field}/{index}",
                    reference,
                    "Evidence references must identify a current evidence record",
                )
    if stream == "events":
        links = payload.get("links", {})
        if isinstance(links, Mapping):
            for target_stream in sorted(links):
                target_ids = links[target_stream]
                if target_stream not in EVENT_LINK_STREAMS:
                    yield _reference_problem(
                        stream,
                        record,
                        f"/payload/links/{target_stream}",
                        target_ids,
                        "Event links may only target canonical concept or archetype streams",
                    )
                    continue
                if not isinstance(target_ids, list):
                    yield _reference_problem(
                        stream,
                        record,
                        f"/payload/links/{target_stream}",
                        target_ids,
                        "Event link targets must be an array of current record IDs",
                    )
                    continue
                for index, target_id in enumerate(target_ids):
                    if target_id not in current.get(target_stream, {}):
                        yield _reference_problem(
                            stream,
                            record,
                            f"/payload/links/{target_stream}/{index}",
                            target_id,
                            f"Event links must identify current {target_stream} records",
                        )
    if stream == "meta_revisions":
        promotion_refs = payload.get("promotion_refs", [])
        if isinstance(promotion_refs, list):
            for index, reference in enumerate(promotion_refs):
                prefix, separator, hypothesis_id = (
                    reference.partition(":")
                    if isinstance(reference, str)
                    else ("", "", "")
                )
                if (
                    prefix != "hypothesis"
                    or not separator
                    or hypothesis_id not in current.get("hypotheses", {})
                ):
                    yield _reference_problem(
                        stream,
                        record,
                        f"/payload/promotion_refs/{index}",
                        reference,
                        "Promotion references must identify a current hypothesis record",
                    )


def _evidence_supports(stream: str, evidence: Mapping[str, Any]) -> bool:
    payload = _payload(evidence)
    source_kind, speaker = payload.get("source_kind"), payload.get("speaker")
    if speaker == "assistant":
        return False
    user_authored = payload.get("user_authored") is True
    user_conversation = (
        source_kind == "conversation" and speaker == "user" and user_authored
    )
    user_registered_material = (
        source_kind == "registered" and user_authored and speaker != "assistant"
    )
    if stream in PERSONAL_FACT_STREAMS:
        return user_conversation or user_registered_material
    if stream in CONCEPT_STREAMS:
        return (
            source_kind == "external"
            or user_conversation
            or source_kind == "registered"
        )
    return True


def validate_evidence_eligibility(
    stream: str,
    record: Mapping[str, Any],
    evidence_current: Mapping[str, Mapping[str, Any]],
) -> Iterable[dict[str, Any]]:
    payload = _payload(record)
    if stream in {"evidence", "sources"}:
        fidelity = payload.get("fidelity")
        if fidelity not in APPROVED_FIDELITIES:
            yield _problem(
                "EVIDENCE_FIDELITY_INVALID",
                "Evidence fidelity must use one approved v0.5 value",
                stream=stream,
                record_id=record.get("id"),
                path="/payload/fidelity",
                value=fidelity,
                recovery=[
                    "Use selected_excerpt, full_user_provided_transcript, or registered_source_excerpt"
                ],
            )
        elif (
            fidelity == "full_user_provided_transcript"
            and payload.get("user_authored") is not True
        ):
            yield _problem(
                "EVIDENCE_FIDELITY_INVALID",
                "Only explicitly user-authored material may claim full transcript fidelity",
                stream=stream,
                record_id=record.get("id"),
                path="/payload/user_authored",
                value=payload.get("user_authored"),
                recovery=[
                    "Use excerpt fidelity or mark only actually user-authored transcript material"
                ],
            )
        return
    for field in EVIDENCE_FIELDS.get(stream, ()):
        refs = payload.get(field, [])
        if not isinstance(refs, list):
            continue
        for index, reference in enumerate(refs):
            if not isinstance(reference, str) or not reference.startswith("evidence:"):
                continue
            evidence = evidence_current.get(reference.removeprefix("evidence:"))
            if evidence is not None and not _evidence_supports(stream, evidence):
                yield _problem(
                    "EVIDENCE_INELIGIBLE",
                    "This evidence cannot independently support the target record under the v0.5 epistemic policy",
                    stream=stream,
                    record_id=record.get("id"),
                    path=f"/payload/{field}/{index}",
                    value=reference,
                    recovery=[
                        "Cite explicitly user-authored personal evidence or use external evidence only for psychology/philosophy concepts"
                    ],
                )


def validate_profile(
    _repo_root: Path, records: dict[str, list[dict[str, Any]]]
) -> Iterable[dict[str, Any]]:
    """Yield deterministic v0.5 cross-record problems without raising domain exceptions."""
    for stream in sorted(records):
        if stream not in CANONICAL_BUSINESS_STREAMS:
            yield _problem(
                "STREAM_NOT_FOUND",
                "Record input contains a non-canonical business stream",
                stream=stream,
                record_id=None,
                path=f"/{stream}",
                value=records[stream],
                recovery=[
                    "Remove the legacy/internal stream from the Profile validator input"
                ],
            )
    normalized = {
        stream: [
            record for record in records.get(stream, []) if isinstance(record, Mapping)
        ]
        for stream in CANONICAL_BUSINESS_STREAMS
        if isinstance(records.get(stream, []), list)
    }
    current = {stream: latest_effective(items) for stream, items in normalized.items()}
    histories = {
        stream: _history_by_revision(items) for stream, items in normalized.items()
    }
    for stream in sorted(normalized):
        for record in sorted(normalized[stream], key=_record_key):
            yield from validate_revision_chain(stream, record, histories[stream])
            yield from validate_commit_ref(stream, record)
            if current.get(stream, {}).get(record.get("id")) is record:
                yield from validate_references(stream, record, current)
                yield from validate_evidence_eligibility(
                    stream, record, current.get("evidence", {})
                )
