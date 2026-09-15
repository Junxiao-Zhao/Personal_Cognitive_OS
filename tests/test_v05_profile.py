from __future__ import annotations

import json
import shutil

import pytest

from mem_core.errors import MemError
from mem_core.profile import Profile
from mem_core.registry import default_registry
from pco.paths import bundled_profile


APPROVED_STREAMS = {
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


def test_v05_profile_has_only_approved_business_streams() -> None:
    """Removing or renaming a canonical business stream must fail this contract."""
    profile = Profile.load(bundled_profile(), default_registry())

    assert profile.version == "0.5.0"
    assert profile.config.application_commit_policy == "pco_proposal"
    assert set(profile.config.streams) == APPROVED_STREAMS


def test_v05_profile_rejects_legacy_stream_name() -> None:
    """Reintroducing a v0.4 continuation stream must remain impossible."""
    profile = Profile.load(bundled_profile(), default_registry())

    with pytest.raises(MemError) as exc:
        profile.stream("continuations")

    assert exc.value.detail.code == "STREAM_NOT_FOUND"


@pytest.mark.parametrize("stream", sorted(APPROVED_STREAMS))
def test_v05_minimal_record_validates(stream: str, minimal_records: dict[str, dict]) -> None:
    """Removing a required v0.5 envelope or payload field must reject the record."""
    profile = Profile.load(bundled_profile(), default_registry())

    profile.validate_record_schema(stream, minimal_records[stream])


def test_revision_requires_previous_revision(minimal_records: dict[str, dict]) -> None:
    """Allowing a revision-2 record without its predecessor would break append-only history."""
    profile = Profile.load(bundled_profile(), default_registry())
    record = {**minimal_records["hypotheses"], "revision": 2}
    record["payload"] = {
        **record["payload"],
        "previous_revision": None,
        "change_summary": "Narrow the claim after new counter-evidence",
    }

    with pytest.raises(MemError) as exc:
        profile.validate_record_schema("hypotheses", record)

    assert exc.value.detail.code == "SCHEMA_VALIDATION_FAILED"


def test_revision_requires_previous_revision_and_change_summary(minimal_records: dict[str, dict]) -> None:
    profile = Profile.load(bundled_profile(), default_registry())
    record = {**minimal_records["hypotheses"], "revision": 2}
    record["payload"] = {**record["payload"], "previous_revision": None, "change_summary": ""}

    with pytest.raises(MemError) as exc:
        profile.validate_record_schema("hypotheses", record)

    assert exc.value.detail.code == "SCHEMA_VALIDATION_FAILED"


def test_v05_stream_payload_enforces_common_revision_contract(tmp_path, minimal_records: dict[str, dict]) -> None:
    """Ignoring a common revision constraint would let divergent payloads into one stream."""
    profile_root = tmp_path / "profile"
    shutil.copytree(bundled_profile(), profile_root)
    common_path = profile_root / "schemas" / "common-revision.schema.json"
    common = json.loads(common_path.read_text(encoding="utf-8"))
    common["$defs"]["revision_payload"]["properties"]["body_markdown"] = {"const": "Common contract body"}
    common_path.write_text(json.dumps(common), encoding="utf-8")
    profile = Profile.load(profile_root, default_registry())

    with pytest.raises(MemError) as exc:
        profile.validate_record_schema("hypotheses", minimal_records["hypotheses"])

    assert exc.value.detail.code == "SCHEMA_VALIDATION_FAILED"
