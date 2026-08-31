from __future__ import annotations

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
