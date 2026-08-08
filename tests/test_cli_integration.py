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
    assert "custom/Other input (Tab in the TUI)" in compact
    assert "bare `No`" in compact


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
