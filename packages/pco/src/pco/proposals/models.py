"""Strict data models for the reviewable proposal manifest."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from mem_core.errors import MemError
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import PROPOSAL_MANIFEST_VERSION
from ..versioning import CompatibilityContract


ReviewOperation = Literal["create", "revise", "retire"]
MaterialFidelity = Literal["full_source", "selected_excerpt"]
HashValue = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


def _normalized_relative_path(value: Any) -> PurePosixPath:
    if not isinstance(value, (str, PurePosixPath)):
        raise ValueError("proposal paths must be strings")
    raw = value.as_posix() if isinstance(value, PurePosixPath) else value
    if not raw or raw == "." or "\\" in raw:
        raise ValueError("proposal paths must be non-empty POSIX relative paths")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("proposal paths cannot be absolute or contain traversal")
    if path.as_posix() != raw:
        raise ValueError("proposal paths must already be normalized")
    return path


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BusinessFile(_StrictModel):
    """One manifest-listed business Markdown operation."""

    path: PurePosixPath
    operation: ReviewOperation

    _normalize_path = field_validator("path", mode="before")(
        _normalized_relative_path
    )


class LocalFileMaterial(_StrictModel):
    """External local text material captured by a no-follow reader."""

    material_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    kind: Literal["local_file"]
    locator: str = Field(min_length=1)
    expected_content_hash: HashValue | None = None
    fidelity: Literal["full_source"]

    @field_validator("locator")
    @classmethod
    def require_file_locator(cls, value: str) -> str:
        if urlsplit(value).scheme.lower() != "file":
            raise ValueError("local_file locator must use the file URI scheme")
        return value


class ProposalArtifactMaterial(_StrictModel):
    """Review-visible text material stored below the proposal's inputs tree."""

    material_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    kind: Literal["proposal_artifact"]
    path: PurePosixPath
    expected_content_hash: HashValue | None = None
    fidelity: MaterialFidelity

    _normalize_path = field_validator("path", mode="before")(
        _normalized_relative_path
    )

    @field_validator("path")
    @classmethod
    def require_inputs_path(cls, value: PurePosixPath) -> PurePosixPath:
        if len(value.parts) < 2 or value.parts[0] != "inputs":
            raise ValueError("proposal artifacts must be beneath inputs/")
        return value


InputMaterial = Annotated[
    LocalFileMaterial | ProposalArtifactMaterial,
    Field(discriminator="kind"),
]


class ProposalManifest(_StrictModel):
    """The complete control manifest for one isolated proposal."""

    schema_version: str = PROPOSAL_MANIFEST_VERSION
    review_id: str = Field(pattern=r"^review_[A-Za-z0-9._-]+$")
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    workspace_version: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    input_material: list[InputMaterial] = Field(default_factory=list)
    files: list[BusinessFile] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_supported_schema_version(cls, value: Any) -> str:
        supported = CompatibilityContract.for_package().proposal_manifest
        compatible = False
        if isinstance(value, str) and value == value.strip():
            try:
                compatible = Version(value) in supported
            except InvalidVersion:
                pass
        if not compatible:
            raise MemError(
                "WORKSPACE_FORMAT_MISMATCH",
                "compatibility",
                f"proposal manifest version {value!r} is outside the supported range {supported}",
                path="proposal manifest",
                value={"observed": value, "supported": str(supported)},
                recovery=["Use a proposal manifest with a compatible v0.5 contract"],
                canonical_changed=False,
                retry_safe=False,
            )
        return value

    @model_validator(mode="after")
    def require_unique_manifest_entries(self) -> "ProposalManifest":
        material_ids = [item.material_id for item in self.input_material]
        if len(material_ids) != len(set(material_ids)):
            raise ValueError("input material IDs must be unique")

        business_paths = [item.path.as_posix() for item in self.files]
        if len(business_paths) != len(set(business_paths)):
            raise ValueError("business file paths must be unique")

        artifact_paths = [
            item.path.as_posix()
            for item in self.input_material
            if isinstance(item, ProposalArtifactMaterial)
        ]
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("proposal artifact paths must be unique")
        if set(business_paths) & set(artifact_paths):
            raise ValueError("a path cannot be both a business file and input material")
        return self
