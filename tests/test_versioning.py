from __future__ import annotations

import pytest
from packaging.version import Version

from mem_core.errors import MemError
from pco.versioning import CompatibilityContract, check_compatibility


def test_compatible_patch_package_accepts_050_contracts() -> None:
    check_compatibility(
        package="0.5.1",
        workspace="0.5.0",
        profile="0.5.0",
        proposal_manifest="0.5.0",
        skills=("0.5.0", "0.5.0"),
    )


def test_contract_declares_the_v05_compatibility_range() -> None:
    contract = CompatibilityContract.for_package("0.5.1")

    for supported in (
        contract.workspace,
        contract.profile,
        contract.proposal_manifest,
        contract.skills,
    ):
        assert Version("0.5.0") in supported
        assert Version("0.5.1") in supported
        assert Version("0.6.0") not in supported


@pytest.mark.parametrize("field", ["workspace", "profile", "proposal_manifest"])
def test_out_of_range_format_contract_has_format_mismatch(field: str) -> None:
    versions = {
        "workspace": "0.5.0",
        "profile": "0.5.0",
        "proposal_manifest": "0.5.0",
    }
    versions[field] = "0.6.0"

    with pytest.raises(MemError) as exc:
        check_compatibility(package="0.5.1", skills=("0.5.0", "0.5.0"), **versions)

    assert exc.value.detail.code == "WORKSPACE_FORMAT_MISMATCH"
    assert exc.value.detail.canonical_changed is False
    assert exc.value.detail.retry_safe is False


def test_out_of_range_skill_contract_has_skill_invalid() -> None:
    with pytest.raises(MemError) as exc:
        check_compatibility(
            package="0.5.1",
            workspace="0.5.0",
            profile="0.5.0",
            proposal_manifest="0.5.0",
            skills=("0.5.0", "0.6.0"),
        )

    assert exc.value.detail.code == "WORKSPACE_SKILL_INVALID"
    assert exc.value.detail.canonical_changed is False
    assert exc.value.detail.retry_safe is False
