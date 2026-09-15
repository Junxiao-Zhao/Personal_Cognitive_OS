from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version

from mem_core.errors import MemError
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from .config import COMPATIBILITY_RANGE


@dataclass(frozen=True, slots=True)
class CompatibilityContract:
    package: Version
    workspace: SpecifierSet
    profile: SpecifierSet
    proposal_manifest: SpecifierSet
    skills: SpecifierSet

    @classmethod
    def for_package(cls, package: str | None = None) -> "CompatibilityContract":
        return cls(
            package=Version(version("pco") if package is None else package),
            workspace=SpecifierSet(COMPATIBILITY_RANGE),
            profile=SpecifierSet(COMPATIBILITY_RANGE),
            proposal_manifest=SpecifierSet(COMPATIBILITY_RANGE),
            skills=SpecifierSet(COMPATIBILITY_RANGE),
        )


def check_compatibility(
    *,
    package: str | None = None,
    workspace: str,
    profile: str,
    proposal_manifest: str,
    skills: tuple[str, str],
) -> None:
    contract = CompatibilityContract.for_package(package)
    _ensure_compatible(
        workspace,
        contract.workspace,
        code="WORKSPACE_FORMAT_MISMATCH",
        name="workspace format",
    )
    _ensure_compatible(
        profile,
        contract.profile,
        code="WORKSPACE_FORMAT_MISMATCH",
        name="Profile",
    )
    _ensure_compatible(
        proposal_manifest,
        contract.proposal_manifest,
        code="WORKSPACE_FORMAT_MISMATCH",
        name="proposal manifest",
    )
    for skill in skills:
        _ensure_compatible(
            skill,
            contract.skills,
            code="WORKSPACE_SKILL_INVALID",
            name="SKILL set",
        )


def _ensure_compatible(
    observed: str,
    supported: SpecifierSet,
    *,
    code: str,
    name: str,
) -> None:
    try:
        compatible = Version(observed) in supported
    except InvalidVersion:
        compatible = False
    if compatible:
        return
    raise MemError(
        code,
        "compatibility",
        f"{name} version {observed!r} is outside the supported range {supported}",
        path=name,
        value={"observed": observed, "supported": str(supported)},
        recovery=["Use a workspace with compatible v0.5 contracts"],
        canonical_changed=False,
        retry_safe=False,
    )
