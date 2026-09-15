from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from pco.proposals.digests import (
    frame,
    input_digest,
    proposal_digest,
    receipt_id,
    transaction_digest,
)
from pco.proposals.markdown import ParsedBusinessObject
from pco.proposals.materials import CapturedMaterial
from pco.proposals.models import LocalFileMaterial, ProposalArtifactMaterial


def _material(material_id: str, text: bytes, *, artifact: bool) -> CapturedMaterial:
    descriptor = (
        ProposalArtifactMaterial(
            material_id=material_id,
            kind="proposal_artifact",
            path=f"inputs/{material_id}.txt",
            expected_content_hash="sha256:" + hashlib.sha256(text).hexdigest(),
            fidelity="selected_excerpt",
        )
        if artifact
        else LocalFileMaterial(
            material_id=material_id,
            kind="local_file",
            locator=f"file:///tmp/{material_id}.txt",
            expected_content_hash="sha256:" + hashlib.sha256(text).hexdigest(),
            fidelity="full_source",
        )
    )
    return CapturedMaterial(
        material=descriptor,
        raw_bytes=text,
        normalized_bytes=text,
        content_hash="sha256:" + hashlib.sha256(text).hexdigest(),
        file_hash=("sha256:" + hashlib.sha256(text).hexdigest()) if artifact else None,
        identity=(1, 2, 3, len(text)),
    )


def _parsed(path: str, *, confidence: float = 0.75) -> ParsedBusinessObject:
    return ParsedBusinessObject(
        stream="hypotheses",
        operation="create",
        record_id="hyp_digest",
        base_revision=None,
        structured={
            "change_summary": "Create hypothesis",
            "confidence": confidence,
            "evidence_refs": ["evidence:ev_one"],
            "counter_evidence_refs": [],
            "status": "hypothesis",
        },
        body_markdown="Semantic body.\n",
        relative_path=PurePosixPath(path),
    )


def test_frame_uses_unsigned_64_bit_big_endian_byte_length() -> None:
    assert frame("é") == b"\x00\x00\x00\x00\x00\x00\x00\x02\xc3\xa9"
    assert frame(b"a\x00b") == b"\x00\x00\x00\x00\x00\x00\x00\x03a\x00b"


def test_receipt_id_matches_independent_framed_golden_vector() -> None:
    expected = "commit_" + hashlib.sha256(
        b"pco-v0.5-proposal-commit\0"
        + b"\x00\x00\x00\x00\x00\x00\x00\x02ws"
        + b"\x00\x00\x00\x00\x00\x00\x00\x02r1"
    ).hexdigest()
    assert receipt_id("ws", "r1") == expected
    assert receipt_id("workspace_a", "review_same") != receipt_id(
        "workspace_b", "review_same"
    )


def test_input_digest_matches_exact_field_order_and_utf8_material_sort() -> None:
    local = _material("z", b"local\n", artifact=False)
    artifact = _material("a", b"excerpt\n", artifact=True)
    expected_bytes = b"pco-input-digest-v0.5\0"
    for values in (
        ("a", "proposal_artifact", "path", "inputs/a.txt", "selected_excerpt", b"excerpt\n"),
        ("z", "local_file", "locator", "file:///tmp/z.txt", "full_source", b"local\n"),
    ):
        expected_bytes += b"".join(frame(value) for value in values)
    expected = "sha256:" + hashlib.sha256(expected_bytes).hexdigest()

    assert input_digest((local, artifact)) == expected
    assert input_digest((artifact, local)) == expected


def test_input_digest_excludes_expected_hash_and_capture_metadata() -> None:
    first = _material("a", b"same\n", artifact=True)
    changed_descriptor = first.material.model_copy(
        update={"expected_content_hash": "sha256:" + "f" * 64}
    )
    second = CapturedMaterial(
        material=changed_descriptor,
        raw_bytes=b"different exact bytes are excluded",
        normalized_bytes=first.normalized_bytes,
        content_hash="sha256:" + "0" * 64,
        file_hash="sha256:" + "1" * 64,
        identity=(99, 98, 97, 96),
    )
    assert input_digest((first,)) == input_digest((second,))


def test_proposal_digest_is_semantic_and_path_ordered_not_format_bound() -> None:
    first = _parsed("hypotheses/z.md")
    second = first.model_copy(update={"relative_path": PurePosixPath("hypotheses/a.md")})
    assert proposal_digest((first,)) == proposal_digest((second,))
    assert proposal_digest((first, second)) == proposal_digest((second, first))
    assert proposal_digest((first,)) != proposal_digest((_parsed("hypotheses/z.md", confidence=0.5),))


def test_transaction_digest_binds_controls_and_ignores_generated_timestamps() -> None:
    operation = {
        "op": "append",
        "stream": "hypotheses",
        "record": {
            "id": "hyp_digest",
            "revision": 1,
            "recorded_at": "2026-01-01T00:00:00+00:00",
            "schema_version": "pco/hypothesis/v1",
            "commit_ref": "commit_" + "a" * 64,
            "payload": {"status": "hypothesis"},
        },
    }
    controls = dict(
        workspace_id="workspace_one",
        base_commit="a" * 40,
        input_digest_value="sha256:" + "1" * 64,
        proposal_digest_value="sha256:" + "2" * 64,
        manifest_hash="sha256:" + "3" * 64,
        receipt_id_value="commit_" + "4" * 64,
        profile_version="0.5.0",
        manifest_schema_version="0.5.0",
        stream_schema_versions={"hypotheses": "pco/hypothesis/v1"},
        operations=[operation],
    )
    baseline = transaction_digest(**controls)
    timestamp_changed = json.loads(json.dumps(operation))
    timestamp_changed["record"]["recorded_at"] = "2099-12-31T23:59:59+00:00"
    assert transaction_digest(**{**controls, "operations": [timestamp_changed]}) == baseline
    assert transaction_digest(**{**controls, "base_commit": "b" * 40}) != baseline
    assert transaction_digest(**{**controls, "profile_version": "0.5.1"}) != baseline
