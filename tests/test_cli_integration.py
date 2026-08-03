from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path

import yaml
import pytest

from mem_core.errors import MemError
from mem_core.models import Operation
from mem_core.transaction import TransactionManager
from pco.cli import _install_opencode
from pco.config import load_config

from conftest import envelope


def test_opencode_integration_installs_all_contract_files(workspace, tmp_path: Path) -> None:
    project = tmp_path / "project"
    result = _install_opencode(workspace, project, force=False)
    installed = {Path(path).relative_to(project).as_posix() for path in result["installed"]}
    assert ".opencode/plugins/pco.ts" in installed
    assert ".opencode/agents/pco-consolidator.md" in installed
    assert ".opencode/commands/compact.md" in installed
    assert ".opencode/skills/pco-memory/SKILL.md" in installed
    plugin = (project / ".opencode/plugins/pco.ts").read_text(encoding="utf-8")
    assert "pco_checkpoint" in plugin
    assert '"chat.message"' in plugin and "普通输入已锁定" in plugin
    assert '"command.execute.before"' in plugin and "metadata?.pco_control" in plugin
    assert 'visible.includes("[PCO_CONTROL]")' not in plugin
    assert '"experimental.chat.system.transform"' in plugin
    assert '"experimental.session.compacting"' in plugin
    assert '"experimental.compaction.autocontinue"' in plugin
    assert "idleTask = task" in plugin and "await task" in plugin
    compact = (project / ".opencode/commands/compact.md").read_text(encoding="utf-8")
    assert "question` tool" in compact
    assert "custom/Other input (Tab in the TUI)" in compact
    assert "bare `No`" in compact


def test_workspace_configuration_survives_restart(tmp_path: Path) -> None:
    from pco.workspace import Workspace

    root = tmp_path / "persisted"
    configured = load_config(workspace=root, overrides=["checkpoint.derivations.projection=markdown"])
    Workspace(configured).init()
    reopened = load_config(workspace=root)
    assert reopened.checkpoint.derivations.projection == "markdown"


def test_workspace_migrates_old_canonical_profile_before_refresh(workspace) -> None:
    profile_file = workspace.config.memory_root / "profiles" / "pco" / "profile.yaml"
    raw = yaml.safe_load(profile_file.read_text(encoding="utf-8"))
    raw["version"] = "0.3.1"
    raw["streams"].pop("search_receipts")
    raw["retrieval"]["rrf_k"] = 17
    raw["retrieval"].pop("candidate_count")
    raw["retrieval"].pop("candidate_overfetch_factor")
    profile_file.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    schema = workspace.config.memory_root / "profiles" / "pco" / "schemas" / "search-receipt.schema.json"
    stream = workspace.config.memory_root / "sources" / "search-receipts.jsonl"
    schema.unlink()
    stream.unlink()
    marker_path = workspace.config.memory_root / ".mem-profile.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["version"] = "0.3.1"
    marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    legacy_concept = envelope(
        "psy_legacy_reference",
        "pco/psychology/v1",
        {
            "name": "旧版概念",
            "description": "在搜索回执强校验之前保存的合法概念。",
            "aliases": [],
            "external_refs": [
                {
                    "url": "https://example.org/legacy-concept",
                    "title": "Legacy concept reference",
                    "accessed_at": "2026-08-03T10:00:00+00:00",
                    "search_receipt": "legacy_free_text_receipt",
                }
            ],
            "status": "active",
        },
    )
    concept_path = workspace.config.memory_root / "structured" / "psychologies.jsonl"
    concept_path.write_text(json.dumps(legacy_concept, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    workspace.repository._git("add", "-A")
    workspace.repository._git("commit", "--no-verify", "-m", "fixture: emulate pco 0.3.1 workspace")
    old_head = workspace.repository.head()

    from pco.workspace import Workspace

    reopened = Workspace(workspace.config)
    reopened.refresh_repository_profile()
    assert reopened.repository.head() != old_head
    assert reopened.profile.version == "0.3.2"
    assert reopened.repository.profile is reopened.profile
    assert "search_receipts" in reopened.profile.config.streams
    assert reopened.profile.raw["retrieval"]["candidate_count"] == 200
    assert reopened.profile.raw["retrieval"]["candidate_overfetch_factor"] == 4
    assert reopened.profile.raw["retrieval"]["rrf_k"] == 17
    assert schema.is_file() and stream.is_file()
    audit = workspace.config.memory_root / "transactions" / "profile-migrations.jsonl"
    migration = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
    assert migration["profile_after"] == "pco@0.3.2"
    assert migration["legacy_external_refs"] == [
        {
            "stream": "psychologies",
            "record_id": "psy_legacy_reference",
            "revision": 1,
            "external_ref_index": 0,
            "url": "https://example.org/legacy-concept",
            "search_receipt": "legacy_free_text_receipt",
        }
    ]
    assert reopened.repository.validate_all()["ok"]

    post_migration = envelope(
        "psy_post_migration_forgery",
        "pco/psychology/v1",
        {
            **legacy_concept["payload"],
            "name": "迁移后伪造概念",
        },
    )
    manager = TransactionManager(reopened.repository, workspace.config.state_root)
    transaction = manager.begin(fingerprint_context={"kind": "post_migration_forgery"})
    manager.append(transaction.id, Operation(op="append", stream="psychologies", record=post_migration))
    with pytest.raises(MemError) as error:
        manager.validate(transaction.id)
    assert error.value.detail.code == "EXTERNAL_REFERENCE_INVALID"
    assert reopened.repository.is_clean()
