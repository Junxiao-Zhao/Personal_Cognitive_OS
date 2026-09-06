from __future__ import annotations

from copy import deepcopy
from pathlib import PurePosixPath

import pytest
from pydantic import ValidationError

from mem_core.errors import MemError
from pco.proposals.markdown import (
    ParsedBusinessObject,
    parse_business_markdown,
    render_business_markdown,
)


HYPOTHESIS_MD = b"""---
type: hypothesis
id: hyp_eval
operation: revise
base_revision: 2
change_summary: Narrow the claim
confidence: 0.45
evidence_refs:
  - evidence:ev_1
counter_evidence_refs: []
status: hypothesis
---

A claim with **reviewable** detail.\r\n"""


class _MutableValue:
    def __init__(self) -> None:
        self.value = "original"


def _assert_invalid(path: str, document: bytes) -> MemError:
    with pytest.raises(MemError) as exc:
        parse_business_markdown(PurePosixPath(path), document)
    assert exc.value.detail.code == "PROPOSAL_SCHEMA_INVALID"
    assert exc.value.detail.canonical_changed is False
    return exc.value


def test_markdown_round_trip_is_semantically_stable() -> None:
    parsed = parse_business_markdown(
        PurePosixPath("hypotheses/hyp_eval.md"), HYPOTHESIS_MD
    )

    rendered = render_business_markdown(parsed.to_record_preview())

    assert parse_business_markdown(
        PurePosixPath("hypotheses/hyp_eval.md"), rendered
    ) == parsed
    assert rendered.endswith(b"A claim with **reviewable** detail.\n")
    assert b"&id" not in rendered and b"!!" not in rendered


def test_parser_returns_closed_typed_business_object() -> None:
    parsed = parse_business_markdown(
        PurePosixPath("hypotheses/hyp_eval.md"), HYPOTHESIS_MD
    )

    assert parsed == ParsedBusinessObject(
        stream="hypotheses",
        operation="revise",
        record_id="hyp_eval",
        base_revision=2,
        structured={
            "change_summary": "Narrow the claim",
            "confidence": 0.45,
            "evidence_refs": ["evidence:ev_1"],
            "counter_evidence_refs": [],
            "status": "hypothesis",
        },
        body_markdown="\nA claim with **reviewable** detail.\n",
        relative_path=PurePosixPath("hypotheses/hyp_eval.md"),
    )


def test_parsed_semantics_are_deeply_immutable(
    minimal_records: dict[str, dict]
) -> None:
    hypothesis = parse_business_markdown(
        PurePosixPath("hypotheses/hyp_eval.md"), HYPOTHESIS_MD
    )
    event = parse_business_markdown(
        PurePosixPath("events/evt_1.md"),
        render_business_markdown(minimal_records["events"]),
    )

    with pytest.raises(TypeError):
        hypothesis.structured["confidence"] = 0.9
    with pytest.raises((AttributeError, TypeError)):
        hypothesis.structured["evidence_refs"].append("evidence:evil")
    with pytest.raises(TypeError):
        event.structured["occurred_at"]["precision"] = "unknown"
    with pytest.raises(TypeError):
        dict.__setitem__(hypothesis.structured, "confidence", 0.9)
    assert hypothesis.structured == {
        "change_summary": "Narrow the claim",
        "confidence": 0.45,
        "evidence_refs": ["evidence:ev_1"],
        "counter_evidence_refs": [],
        "status": "hypothesis",
    }


def test_preview_is_deeply_detached_from_parsed_semantics() -> None:
    parsed = parse_business_markdown(
        PurePosixPath("hypotheses/hyp_eval.md"), HYPOTHESIS_MD
    )
    preview = parsed.to_record_preview()

    preview["payload"]["confidence"] = 0.9
    preview["payload"]["evidence_refs"].append("evidence:evil")

    assert parsed.structured["confidence"] == 0.45
    assert parsed.structured["evidence_refs"] == ("evidence:ev_1",)


@pytest.mark.parametrize(
    "structured",
    [
        {"unsafe": bytearray(b"a")},
        {"unsafe": {"mutable"}},
        {"unsafe": _MutableValue()},
        {"unsafe": float("nan")},
        {1: "non-string key"},
        {"unsafe": ("tuple",)},
    ],
    ids=["bytearray", "set", "custom-object", "nonfinite", "key", "tuple"],
)
def test_direct_construction_rejects_non_json_structured_values(
    structured: dict,
) -> None:
    with pytest.raises(ValidationError) as exc:
        ParsedBusinessObject(
            stream="sources",
            operation="create",
            record_id="src_direct",
            base_revision=None,
            structured=structured,
            body_markdown="body\n",
            relative_path=PurePosixPath("sources/src_direct.md"),
        )

    assert exc.value.errors()[0]["loc"] == ("structured",)


def test_direct_construction_detaches_valid_nested_json_values() -> None:
    structured = {
        "change_summary": "Create event",
        "occurred_at": {"precision": "day", "start": "2026-08-30"},
        "evidence_refs": ["evidence:ev_1"],
        "status": "active",
    }
    parsed = ParsedBusinessObject(
        stream="events",
        operation="create",
        record_id="evt_direct",
        base_revision=None,
        structured=structured,
        body_markdown="body\n",
        relative_path=PurePosixPath("events/evt_direct.md"),
    )

    structured["occurred_at"]["precision"] = "unknown"
    structured["evidence_refs"].append("evidence:evil")

    expected = {
        "change_summary": "Create event",
        "occurred_at": {"precision": "day", "start": "2026-08-30"},
        "evidence_refs": ["evidence:ev_1"],
        "status": "active",
    }
    assert parsed.structured == expected
    assert parsed.model_dump(mode="json")["structured"] == expected
    preview = parsed.to_record_preview()
    assert preview["payload"]["occurred_at"] == expected["occurred_at"]
    assert preview["payload"]["evidence_refs"] == expected["evidence_refs"]


@pytest.mark.parametrize(
    "document",
    [
        HYPOTHESIS_MD.replace(b"confidence: 0.45", b"confidence: 0.45\nconfidence: 0.8"),
        HYPOTHESIS_MD.replace(b"confidence: 0.45", b"unknown: nope"),
        HYPOTHESIS_MD.replace(b"confidence: 0.45", b"confidence: !!python/object/apply:os.system ['false']"),
        HYPOTHESIS_MD.replace(b"confidence: 0.45", b"confidence: &score 0.45\ncopy: *score"),
        HYPOTHESIS_MD.replace(b"confidence: 0.45", b"? [not, a, string]\n: value"),
        HYPOTHESIS_MD.replace(b"confidence: 0.45", b"confidence: .nan"),
        HYPOTHESIS_MD.replace(b"confidence: 0.45", b"revision: 4"),
        HYPOTHESIS_MD.replace(b"confidence: 0.45", b"previous_revision: 1"),
        HYPOTHESIS_MD.replace(b"confidence: 0.45", b"recorded_at: 2026-08-30"),
        HYPOTHESIS_MD.replace(b"confidence: 0.45", b"schema_version: pco/hypothesis/v1"),
        HYPOTHESIS_MD.replace(b"confidence: 0.45", b"commit_ref: commit_bad"),
        HYPOTHESIS_MD.replace(b"confidence: 0.45", b"confidence: 0.45\n..."),
    ],
)
def test_unsafe_unknown_generated_or_multidoc_frontmatter_is_rejected(
    document: bytes,
) -> None:
    _assert_invalid("hypotheses/hyp_eval.md", document)


@pytest.mark.parametrize(
    ("path", "object_type", "record_id", "expected_stream"),
    [
        ("evidence/ev_1.md", "evidence", "ev_1", "evidence"),
        ("sources/src_1.md", "source", "src_1", "sources"),
        ("events/evt_1.md", "event", "evt_1", "events"),
        ("psychologies/psy_1.md", "psychology", "psy_1", "psychologies"),
        ("philosophies/phi_1.md", "philosophy", "phi_1", "philosophies"),
        ("archetypes/arc_1.md", "archetype", "arc_1", "archetypes"),
        ("hypotheses/hyp_1.md", "hypothesis", "hyp_1", "hypotheses"),
        ("META_MEMORY.md", "meta", "meta_current", "meta_revisions"),
        ("SESSION_HANDOFF.md", "session_handoff", "handoff_1", "session_handoffs"),
    ],
)
def test_every_business_path_maps_to_exact_type_and_stream(
    minimal_records: dict[str, dict],
    path: str,
    object_type: str,
    record_id: str,
    expected_stream: str,
) -> None:
    record = deepcopy(minimal_records[expected_stream])
    record["id"] = record_id
    document = render_business_markdown(record)

    parsed = parse_business_markdown(PurePosixPath(path), document)

    assert parsed.stream == expected_stream
    assert parsed.record_id == record_id


@pytest.mark.parametrize(
    ("stream", "directory"),
    [
        ("evidence", "evidence"),
        ("sources", "sources"),
        ("events", "events"),
        ("psychologies", "psychologies"),
        ("philosophies", "philosophies"),
        ("archetypes", "archetypes"),
        ("hypotheses", "hypotheses"),
    ],
)
def test_profile_valid_unicode_and_colon_ids_round_trip_through_matching_paths(
    minimal_records: dict[str, dict], stream: str, directory: str
) -> None:
    record = deepcopy(minimal_records[stream])
    record["id"] = "对象:一"

    rendered = render_business_markdown(record)
    parsed = parse_business_markdown(
        PurePosixPath(f"{directory}/对象:一.md"), rendered
    )

    assert parsed.record_id == "对象:一"
    assert parsed.to_record_preview()["id"] == "对象:一"


@pytest.mark.parametrize(
    ("stream", "path", "field", "references"),
    [
        (
            "hypotheses",
            "hypotheses/hyp_1.md",
            "evidence_refs",
            ["evidence:证据", "evidence:ev:one"],
        ),
        (
            "meta_revisions",
            "META_MEMORY.md",
            "promotion_refs",
            ["hypothesis:假设", "hypothesis:hyp:one"],
        ),
    ],
)
def test_typed_references_preserve_unicode_and_colons_in_target_ids(
    minimal_records: dict[str, dict],
    stream: str,
    path: str,
    field: str,
    references: list[str],
) -> None:
    record = deepcopy(minimal_records[stream])
    record["payload"][field] = references

    parsed = parse_business_markdown(
        PurePosixPath(path), render_business_markdown(record)
    )

    assert parsed.to_record_preview()["payload"][field] == references


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        ("events/evt_wrong.md", None),
        ("hypotheses/hyp_eval.txt", None),
        ("events/nested/evt_1.md", None),
        ("META_MEMORY.md", (b"type: hypothesis", b"type: meta")),
        ("SESSION_HANDOFF.md", (b"type: hypothesis", b"type: session_handoff")),
    ],
)
def test_path_filename_and_type_mismatches_are_rejected(
    path: str, replacement: tuple[bytes, bytes] | None
) -> None:
    document = HYPOTHESIS_MD
    if replacement is not None:
        document = document.replace(*replacement)
    _assert_invalid(path, document)


@pytest.mark.parametrize(
    "prefix",
    [b"", b"heading\n", b"--- \n", b"...\n"],
)
def test_exact_leading_and_terminating_frontmatter_are_required(prefix: bytes) -> None:
    document = HYPOTHESIS_MD if prefix == b"" else prefix + HYPOTHESIS_MD
    if prefix == b"":
        document = document.replace(b"---\n\nA claim", b"...\n\nA claim", 1)
    _assert_invalid("hypotheses/hyp_eval.md", document)


def test_body_uses_strict_material_normalization() -> None:
    parsed = parse_business_markdown(
        PurePosixPath("hypotheses/hyp_eval.md"),
        HYPOTHESIS_MD.replace(
            b"A claim with **reviewable** detail.\r\n",
            b"line one\rline two\r\n",
        ),
    )
    assert parsed.body_markdown == "\nline one\nline two\n"

    _assert_invalid(
        "hypotheses/hyp_eval.md",
        HYPOTHESIS_MD.replace(b"A claim", b"A\x00 claim"),
    )
    _assert_invalid(
        "hypotheses/hyp_eval.md",
        HYPOTHESIS_MD.replace(b"A claim", b"A\xff claim"),
    )


def test_deeply_nested_flow_yaml_returns_stable_schema_error() -> None:
    nested = b"[" * 1_200 + b"value" + b"]" * 1_200
    document = HYPOTHESIS_MD.replace(
        b"counter_evidence_refs: []",
        b"counter_evidence_refs: " + nested,
    )

    error = _assert_invalid("hypotheses/hyp_eval.md", document)

    assert error.detail.canonical_changed is False


@pytest.mark.parametrize(
    ("operation", "base_revision", "status"),
    [
        ("create", "base_revision: 1\n", "hypothesis"),
        ("revise", "", "hypothesis"),
        ("revise", "base_revision: 0\n", "hypothesis"),
        ("retire", "", "tombstone"),
        ("retire", "base_revision: 2\n", "hypothesis"),
        ("create", "", "tombstone"),
    ],
)
def test_operation_base_revision_and_tombstone_shape_are_strict(
    operation: str, base_revision: str, status: str
) -> None:
    document = HYPOTHESIS_MD.replace(b"operation: revise\n", f"operation: {operation}\n".encode())
    document = document.replace(b"base_revision: 2\n", base_revision.encode())
    document = document.replace(b"status: hypothesis", f"status: {status}".encode())
    _assert_invalid("hypotheses/hyp_eval.md", document)


@pytest.mark.parametrize(
    "mutation",
    [
        (b"change_summary: Narrow the claim", b"change_summary: ''"),
        (b"evidence:ev_1", b"evt_1"),
        (b"evidence:ev_1", b"evidence:"),
        (b"evidence_refs:\n  - evidence:ev_1", b"evidence_refs:\n  - evidence:ev_1\n  - evidence:ev_1"),
    ],
)
def test_invalid_shape_or_reference_is_rejected(mutation: tuple[bytes, bytes]) -> None:
    _assert_invalid("hypotheses/hyp_eval.md", HYPOTHESIS_MD.replace(*mutation))


def test_source_generated_snapshot_fields_cannot_be_authored(
    minimal_records: dict[str, dict]
) -> None:
    document = render_business_markdown(minimal_records["sources"])
    assert b"content_hash:" not in document
    assert b"snapshot_ref:" not in document

    for field in (b"content_hash: sha256:" + b"a" * 64, b"snapshot_ref: source_blobs/" + b"a" * 64):
        header, body = document.rsplit(b"---\n", 1)
        _assert_invalid(
            "sources/src_1.md",
            header + field + b"\n---\n" + body,
        )


def test_source_material_binding_is_separate_from_canonical_round_trip(
    minimal_records: dict[str, dict]
) -> None:
    canonical_markdown = render_business_markdown(minimal_records["sources"])
    proposal_markdown = canonical_markdown.replace(
        b"operation: create\n",
        b"operation: create\nmaterial_id: journal_current\n",
        1,
    )

    proposed = parse_business_markdown(
        PurePosixPath("sources/src_1.md"), proposal_markdown
    )
    preview = proposed.to_record_preview()
    rendered = render_business_markdown(preview)
    reparsed = parse_business_markdown(PurePosixPath("sources/src_1.md"), rendered)

    assert proposed.material_id == "journal_current"
    assert "material_id" not in proposed.structured
    assert "material_id" not in preview["payload"]
    assert reparsed.material_id is None
    assert reparsed.structured == proposed.structured


def test_renderer_safely_quotes_yaml_like_strings_and_preserves_nested_values(
    minimal_records: dict[str, dict]
) -> None:
    record = deepcopy(minimal_records["events"])
    record["payload"]["change_summary"] = "yes"
    record["payload"]["occurred_at"] = {
        "precision": "natural",
        "start": None,
        "natural": "2026-08-30: after --- lunch",
    }
    record["payload"]["links"] = {
        "psychologies": ["psy_1"],
        "philosophies": [],
        "archetypes": [],
    }

    rendered = render_business_markdown(record)
    parsed = parse_business_markdown(PurePosixPath("events/evt_1.md"), rendered)

    assert parsed.structured["change_summary"] == "yes"
    assert parsed.structured["occurred_at"] == record["payload"]["occurred_at"]
    assert parsed.structured["links"] == record["payload"]["links"]


def test_renderer_is_byte_deterministic_for_equivalent_mapping_order(
    minimal_records: dict[str, dict]
) -> None:
    first = deepcopy(minimal_records["events"])
    first["payload"]["occurred_at"] = {
        "precision": "natural",
        "start": None,
        "natural": "after lunch",
    }
    second = deepcopy(first)
    second["payload"]["occurred_at"] = {
        "natural": "after lunch",
        "start": None,
        "precision": "natural",
    }

    assert render_business_markdown(first) == render_business_markdown(second)


def test_render_rejects_unknown_or_unsafe_record_shapes(minimal_records: dict[str, dict]) -> None:
    unknown = deepcopy(minimal_records["hypotheses"])
    unknown["payload"]["unknown"] = "hidden"
    _assert_render_invalid(unknown)

    unsafe = deepcopy(minimal_records["hypotheses"])
    unsafe["payload"]["confidence"] = object()
    _assert_render_invalid(unsafe)


def test_render_rejects_deeply_nested_record_with_stable_schema_error(
    minimal_records: dict[str, dict]
) -> None:
    record = deepcopy(minimal_records["sources"])
    nested: object = "value"
    for _ in range(1_200):
        nested = {"level": nested}
    record["payload"]["query_context"] = nested

    _assert_render_invalid(record)


@pytest.mark.parametrize(
    ("revision", "previous_revision"),
    [(1, 1), (3, 1)],
)
def test_render_rejects_non_adjacent_revision_envelopes(
    minimal_records: dict[str, dict], revision: int, previous_revision: int
) -> None:
    record = deepcopy(minimal_records["hypotheses"])
    record["revision"] = revision
    record["payload"]["previous_revision"] = previous_revision

    _assert_render_invalid(record)


def _assert_render_invalid(record: dict) -> None:
    with pytest.raises(MemError) as exc:
        render_business_markdown(record)
    assert exc.value.detail.code == "PROPOSAL_SCHEMA_INVALID"
