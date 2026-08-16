from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path

import pytest

from mem_core.errors import MemError
from pco.cli import _install_opencode
from pco.config import load_config


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
    assert "custom/Other reason" in compact
    assert "/pco-yes" not in compact and "/pco-no" not in compact


def test_opencode_upgrade_removes_only_managed_legacy_commands(workspace, tmp_path: Path) -> None:
    project = tmp_path / "project"
    commands = project / ".opencode" / "commands"
    commands.mkdir(parents=True)
    (commands / "pco-yes.md").write_text("legacy yes", encoding="utf-8")
    (commands / "pco-no.md").write_text("legacy no", encoding="utf-8")
    user_command = commands / "user-command.md"
    user_command.write_text("user-owned", encoding="utf-8")

    result = _install_opencode(workspace, project, force=False)

    assert not (commands / "pco-yes.md").exists()
    assert not (commands / "pco-no.md").exists()
    assert user_command.read_text(encoding="utf-8") == "user-owned"
    removed = {Path(path).relative_to(project).as_posix() for path in result["removed_legacy"]}
    assert removed == {".opencode/commands/pco-yes.md", ".opencode/commands/pco-no.md"}
    manifest = json.loads((project / ".opencode" / ".pco-managed.json").read_text(encoding="utf-8"))
    assert "commands/compact.md" in manifest["files"]
    assert "commands/user-command.md" not in manifest["files"]


def test_opencode_install_conflict_does_not_delete_legacy_files(workspace, tmp_path: Path) -> None:
    project = tmp_path / "project"
    commands = project / ".opencode" / "commands"
    commands.mkdir(parents=True)
    legacy = commands / "pco-yes.md"
    legacy.write_text("legacy yes", encoding="utf-8")
    compact = commands / "compact.md"
    compact.write_text("user modified compact", encoding="utf-8")

    with pytest.raises(MemError) as exc_info:
        _install_opencode(workspace, project, force=False)

    assert exc_info.value.detail.code == "OPENCODE_INTEGRATION_CONFLICT"
    assert legacy.read_text(encoding="utf-8") == "legacy yes"
    assert compact.read_text(encoding="utf-8") == "user modified compact"


def test_opencode_install_rejects_manifest_path_escape(workspace, tmp_path: Path) -> None:
    project = tmp_path / "project"
    opencode = project / ".opencode"
    opencode.mkdir(parents=True)
    outside = project / "outside.txt"
    outside.write_text("must survive", encoding="utf-8")
    (opencode / ".pco-managed.json").write_text(
        json.dumps({"version": 1, "files": {"../outside.txt": "sha256:unused"}}),
        encoding="utf-8",
    )

    with pytest.raises(MemError) as exc_info:
        _install_opencode(workspace, project, force=False)

    assert exc_info.value.detail.code == "OPENCODE_MANIFEST_INVALID"
    assert outside.read_text(encoding="utf-8") == "must survive"


def test_workspace_configuration_survives_restart(tmp_path: Path) -> None:
    from pco.workspace import Workspace

    root = tmp_path / "persisted"
    configured = load_config(workspace=root, overrides=["checkpoint.derivations.projection=markdown"])
    Workspace(configured).init()
    reopened = load_config(workspace=root)
    assert reopened.checkpoint.derivations.projection == "markdown"


def test_old_profile_version_fails_with_clear_error(workspace) -> None:
    from pco.workspace import Workspace

    marker_path = workspace.config.memory_root / ".mem-profile.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["version"] = "0.3.1"
    marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    workspace.repository._git("add", ".mem-profile.json")
    workspace.repository._git("commit", "--no-verify", "-m", "fixture: emulate unsupported profile version")
    with pytest.raises(MemError) as exc_info:
        Workspace(workspace.config).refresh_repository_profile()
    assert exc_info.value.detail.code == "PROFILE_MARKER_MISMATCH"
