from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository
from pco.paths import bundled_profile
from pco.proposals.models import (
    BusinessFile,
    ProposalArtifactMaterial,
    ProposalManifest,
)
from pco.proposals.service import ProposalService
from pco.workspace import Workspace

NOW = "2026-08-30T10:00:00+00:00"


def make_public_logs_profile(root: Path) -> Path:
    schema_root = root / "schemas"
    schema_root.mkdir(parents=True)
    (schema_root / "log.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": [
                    "id",
                    "revision",
                    "recorded_at",
                    "schema_version",
                    "payload",
                ],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "revision": {"type": "integer", "minimum": 1},
                    "recorded_at": {"type": "string", "format": "date-time"},
                    "schema_version": {"const": "public/log/v1"},
                    "payload": {
                        "type": "object",
                        "required": ["message"],
                        "properties": {"message": {"type": "string"}},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "profile.yaml").write_text(
        "\n".join(
            [
                "name: public-logs",
                "version: 1.0.0",
                "streams:",
                "  logs:",
                "    path: raw/logs.jsonl",
                "    schema: schemas/log.schema.json",
                "    schema_version: public/log/v1",
                "    write_policy: auto",
                "    validation:",
                "      transaction_mode: delta_only",
                "      run_cross_validators: false",
                "artifact_roots: []",
                "validators: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def repository(tmp_path: Path) -> MemoryRepository:
    profile = Profile.load(make_public_logs_profile(tmp_path / "profile"))
    result = MemoryRepository(tmp_path / "canonical", profile)
    result.init()
    return result


@pytest.fixture
def minimal_records() -> dict[str, dict]:
    """One hand-authored schema-valid create record for every v0.5 stream."""
    envelope = {
        "revision": 1,
        "recorded_at": NOW,
        "commit_ref": "proposal:receipt_1",
    }
    revision = {
        "previous_revision": None,
        "change_summary": "Create canonical record",
        "body_markdown": "A reviewed canonical statement.",
    }
    digest = "a" * 64
    return {
        "evidence": {
            **envelope,
            "id": "ev_1",
            "schema_version": "pco/evidence/v1",
            "payload": {
                **revision,
                "speaker": "user",
                "source_kind": "conversation",
                "user_authored": True,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
        },
        "sources": {
            **envelope,
            "id": "src_1",
            "schema_version": "pco/source/v1",
            "payload": {
                **revision,
                "locator": "file:///journal.md",
                "title": "Journal",
                "retrieved_at": NOW,
                "provider_label": "local file",
                "content_hash": f"sha256:{digest}",
                "snapshot_ref": f"source_blobs/{digest}",
                "fidelity": "registered_source_excerpt",
                "used_excerpts": ["A reviewed excerpt."],
                "status": "active",
            },
        },
        "events": {
            **envelope,
            "id": "evt_1",
            "schema_version": "pco/event/v1",
            "payload": {
                **revision,
                "occurred_at": {"precision": "day", "start": "2026-08-30"},
                "evidence_refs": ["evidence:ev_1"],
                "status": "active",
            },
        },
        "psychologies": {
            **envelope,
            "id": "psy_1",
            "schema_version": "pco/psychology/v1",
            "payload": {
                **revision,
                "name": "Reflection",
                "aliases": [],
                "evidence_refs": ["evidence:ev_1"],
                "status": "active",
            },
        },
        "philosophies": {
            **envelope,
            "id": "phi_1",
            "schema_version": "pco/philosophy/v1",
            "payload": {
                **revision,
                "name": "Pragmatism",
                "aliases": [],
                "evidence_refs": ["evidence:ev_1"],
                "status": "active",
            },
        },
        "archetypes": {
            **envelope,
            "id": "arc_1",
            "schema_version": "pco/archetype/v1",
            "payload": {
                **revision,
                "name": "The Builder",
                "aliases": [],
                "stance": "identify",
                "evidence_refs": ["evidence:ev_1"],
                "status": "active",
            },
        },
        "hypotheses": {
            **envelope,
            "id": "hyp_1",
            "schema_version": "pco/hypothesis/v1",
            "payload": {
                **revision,
                "confidence": 0.45,
                "evidence_refs": ["evidence:ev_1"],
                "counter_evidence_refs": [],
                "status": "hypothesis",
            },
        },
        "meta_revisions": {
            **envelope,
            "id": "meta_current",
            "schema_version": "pco/meta-revision/v1",
            "payload": {
                **revision,
                "evidence_refs": ["evidence:ev_1"],
                "promotion_refs": [],
                "status": "active",
            },
        },
        "session_handoffs": {
            **envelope,
            "id": "handoff_1",
            "schema_version": "pco/session-handoff/v1",
            "payload": {
                **revision,
                "topics": ["Canonical profile migration"],
                "unanswered_questions": [],
                "active_tensions": [],
                "recent_decisions": [],
                "next_directions": ["Validate the new contract"],
                "status": "active",
            },
        },
    }


@pytest.fixture
def v05_repository(tmp_path: Path) -> MemoryRepository:
    profile = Profile.load(bundled_profile(), default_registry())
    result = MemoryRepository(tmp_path / "canonical", profile)
    result.init()
    return result


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "pco"
    Workspace.init(root)
    return Workspace.open(root)


@pytest.fixture
def proposal_factory(workspace: Workspace):
    """Create only the active review root needed by proposal-path tests."""

    def create(review_id: str) -> Path:
        root = workspace.paths.proposals / review_id
        root.mkdir()
        return root

    return create


@pytest.fixture
def business_markdown_factory():
    """Build strict-frontmatter Markdown bytes without legacy checkpoint state."""

    def create(
        *,
        object_type: str,
        record_id: str,
        operation: str = "create",
        base_revision: int | None = None,
        body: str = "A reviewed canonical statement.",
        fields: dict[str, Any] | None = None,
    ) -> bytes:
        frontmatter: dict[str, Any] = {
            "type": object_type,
            "id": record_id,
            "operation": operation,
        }
        if base_revision is not None:
            frontmatter["base_revision"] = base_revision
        frontmatter.update(fields or {})
        lines = ["---"]
        lines.extend(
            f"{key}: {json.dumps(value, ensure_ascii=False)}"
            for key, value in frontmatter.items()
        )
        lines.extend(["---", "", body])
        return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")

    return create


@pytest.fixture
def active_proposal_tree(workspace: Workspace, proposal_factory):
    """Return a public v0.5 workspace and its single empty active review root."""

    return workspace, proposal_factory("review_active")


@pytest.fixture
def committed_workspace(
    workspace: Workspace,
    minimal_records: dict[str, dict],
    business_markdown_factory,
) -> Workspace:
    """A full v0.5 canonical state created only through the public proposal API."""

    root = workspace.paths.proposals / "review_projection"
    root.mkdir()
    material = b"Complete reviewed source material.\n"
    (root / "inputs").mkdir()
    (root / "inputs/source.txt").write_bytes(material)

    paths = {
        "evidence": "evidence/ev_1.md",
        "sources": "sources/src_1.md",
        "events": "events/evt_1.md",
        "psychologies": "psychologies/psy_1.md",
        "philosophies": "philosophies/phi_1.md",
        "archetypes": "archetypes/arc_1.md",
        "hypotheses": "hypotheses/hyp_1.md",
        "meta_revisions": "META_MEMORY.md",
        "session_handoffs": "SESSION_HANDOFF.md",
    }
    object_types = {
        "evidence": "evidence",
        "sources": "source",
        "events": "event",
        "psychologies": "psychology",
        "philosophies": "philosophy",
        "archetypes": "archetype",
        "hypotheses": "hypothesis",
        "meta_revisions": "meta",
        "session_handoffs": "session_handoff",
    }
    files: list[BusinessFile] = []
    for stream, relative in paths.items():
        record = deepcopy(minimal_records[stream])
        payload = dict(record["payload"])
        body = payload.pop("body_markdown")
        payload.pop("previous_revision")
        if stream == "sources":
            payload.pop("content_hash")
            payload.pop("snapshot_ref")
            payload["material_id"] = "source_material"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            business_markdown_factory(
                object_type=object_types[stream],
                record_id=record["id"],
                fields=payload,
                body=body,
            )
        )
        files.append(BusinessFile(path=relative, operation="create"))

    manifest = ProposalManifest(
        review_id=root.name,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=files,
        input_material=[
            ProposalArtifactMaterial(
                material_id="source_material",
                kind="proposal_artifact",
                path="inputs/source.txt",
                expected_content_hash="sha256:" + hashlib.sha256(material).hexdigest(),
                fidelity="full_source",
            )
        ],
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result = ProposalService(workspace).commit(root.name)
    assert result["canonical_status"] == "committed"
    return workspace


def _commit_business_documents(
    workspace: Workspace,
    business_markdown_factory,
    *,
    review_id: str,
    documents: list[dict[str, Any]],
) -> None:
    """Append fixture revisions through the same public proposal boundary."""

    root = workspace.paths.proposals / review_id
    root.mkdir()
    files: list[BusinessFile] = []
    for document in documents:
        relative = document["path"]
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        operation = document.get("operation", "create")
        destination.write_bytes(
            business_markdown_factory(
                object_type=document["type"],
                record_id=document["id"],
                operation=operation,
                base_revision=document.get("base_revision"),
                fields=document["fields"],
                body=document["body"],
            )
        )
        files.append(BusinessFile(path=relative, operation=operation))
    manifest = ProposalManifest(
        review_id=review_id,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=files,
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result = ProposalService(workspace).commit(review_id)
    assert result["canonical_status"] == "committed"


@pytest.fixture
def workspace_with_tombstone(
    committed_workspace: Workspace, business_markdown_factory
) -> Workspace:
    _commit_business_documents(
        committed_workspace,
        business_markdown_factory,
        review_id="review_retrieval_tombstone",
        documents=[
            {
                "path": "hypotheses/hyp_1.md",
                "type": "hypothesis",
                "id": "hyp_1",
                "operation": "retire",
                "base_revision": 1,
                "fields": {
                    "change_summary": "Retire the hypothesis",
                    "confidence": 0.45,
                    "evidence_refs": ["evidence:ev_1"],
                    "counter_evidence_refs": [],
                    "status": "tombstone",
                },
                "body": "This hypothesis is retired.",
            }
        ],
    )
    return committed_workspace


@pytest.fixture
def workspace_with_handoffs(
    committed_workspace: Workspace, business_markdown_factory
) -> Workspace:
    _commit_business_documents(
        committed_workspace,
        business_markdown_factory,
        review_id="review_retrieval_handoff",
        documents=[
            {
                "path": "SESSION_HANDOFF.md",
                "type": "session_handoff",
                "id": "handoff_2",
                "fields": {
                    "change_summary": "Record the latest handoff",
                    "topics": ["Latest topic"],
                    "unanswered_questions": [],
                    "active_tensions": [],
                    "recent_decisions": [],
                    "next_directions": ["Continue from here"],
                    "status": "active",
                },
                "body": "Latest continuity context.",
            }
        ],
    )
    return committed_workspace


@pytest.fixture
def workspace_many_records(
    committed_workspace: Workspace, business_markdown_factory
) -> Workspace:
    _commit_business_documents(
        committed_workspace,
        business_markdown_factory,
        review_id="review_retrieval_many",
        documents=[
            {
                "path": f"hypotheses/hyp_many_{index:03d}.md",
                "type": "hypothesis",
                "id": f"hyp_many_{index:03d}",
                "fields": {
                    "change_summary": "Create bounded fallback fixture record",
                    "confidence": 0.4,
                    "evidence_refs": ["evidence:ev_1"],
                    "counter_evidence_refs": [],
                    "status": "hypothesis",
                },
                "body": f"Repeated retrieval pattern {index:03d}.",
            }
            for index in range(25)
        ],
    )
    return committed_workspace


def _write_cli_hypothesis_proposal(
    workspace: Workspace,
    business_markdown_factory,
    *,
    review_id: str,
) -> ProposalManifest:
    """Write the smallest valid evidence-backed proposal used by CLI tests."""

    root = workspace.paths.proposals / review_id
    root.mkdir()
    evidence_path = "evidence/ev_cli.md"
    hypothesis_path = "hypotheses/hyp_1.md"
    (root / "evidence").mkdir()
    (root / evidence_path).write_bytes(
        business_markdown_factory(
            object_type="evidence",
            record_id="ev_cli",
            fields={
                "change_summary": "Record CLI fixture evidence",
                "speaker": "user",
                "source_kind": "conversation",
                "user_authored": True,
                "fidelity": "selected_excerpt",
                "status": "active",
            },
            body="A reviewed observation for the CLI fixture.",
        )
    )
    (root / "hypotheses").mkdir()
    (root / hypothesis_path).write_bytes(
        business_markdown_factory(
            object_type="hypothesis",
            record_id="hyp_1",
            fields={
                "change_summary": "Create the CLI fixture hypothesis",
                "confidence": 0.6,
                "evidence_refs": ["evidence:ev_cli"],
                "counter_evidence_refs": [],
                "status": "hypothesis",
            },
            body="A hypothesis available through the public history command.",
        )
    )
    manifest = ProposalManifest(
        review_id=review_id,
        base_commit=workspace.repository.head(),
        workspace_version=workspace.identity["workspace_format_version"],
        profile_version=workspace.profile.version,
        files=[
            BusinessFile(path=evidence_path, operation="create"),
            BusinessFile(path=hypothesis_path, operation="create"),
        ],
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


@pytest.fixture
def workspace_with_hypothesis(
    workspace: Workspace, business_markdown_factory
) -> Workspace:
    """A fresh workspace containing only history's required business records."""

    manifest = _write_cli_hypothesis_proposal(
        workspace,
        business_markdown_factory,
        review_id="review_cli_history",
    )
    result = ProposalService(workspace).commit(manifest.review_id)
    assert result["canonical_status"] == "committed"
    return workspace


@pytest.fixture
def workspace_with_proposal(
    workspace: Workspace, business_markdown_factory
) -> tuple[Workspace, ProposalManifest]:
    """A distinct fresh workspace with one matching active valid proposal."""

    manifest = _write_cli_hypothesis_proposal(
        workspace,
        business_markdown_factory,
        review_id="review_cli_active",
    )
    return workspace, manifest
