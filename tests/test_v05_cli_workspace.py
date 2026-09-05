from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from pco.cli import build_parser, main, run
from pco.workspace import Workspace


def _snapshot_tree(root: Path) -> dict[str, tuple[Any, ...]]:
    """Capture the workspace tree without following links."""
    snapshot: dict[str, tuple[Any, ...]] = {}
    pending = [(root, Path("."))]
    while pending:
        directory, prefix = pending.pop()
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            relative = (prefix / entry.name).as_posix()
            info = entry.stat(follow_symlinks=False)
            snapshot[relative] = (
                stat.S_IFMT(info.st_mode),
                stat.S_IMODE(info.st_mode),
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            if stat.S_ISDIR(info.st_mode):
                pending.append((path, prefix / entry.name))
    return snapshot


def test_workspace_cli_slice_contains_no_v04_commands() -> None:
    """Restoring a legacy command would expose unsupported mutable surfaces."""
    help_text = build_parser().format_help()

    assert "init" in help_text and "doctor" in help_text and "verify" in help_text
    for removed in ("run", "install-opencode", "sync", "checkpoint", "compact", "export"):
        assert removed not in help_text
    assert "--workspace" not in help_text


def test_pco_init_defaults_to_current_directory(
    tmp_path: Path, monkeypatch
) -> None:
    """Defaulting init to `.pco` would create a workspace in the wrong place."""
    monkeypatch.chdir(tmp_path)

    assert run(build_parser().parse_args(["init"]))["workspace"] == str(tmp_path)


def test_successful_workspace_command_returns_zero_and_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Returning None or unstructured output would break command automation."""
    monkeypatch.chdir(tmp_path)

    assert main(["init"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["workspace"] == str(tmp_path)
    assert set(result) == {"ok", "workspace", "workspace_id", "memory_commit"}


def test_doctor_discovers_nearest_safe_workspace_ancestor(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Looking only in CWD would make normal nested workspace use fail."""
    root = tmp_path / "workspace"
    Workspace.init(root)
    nested = root / "project" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert main(["doctor"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["identity"]["workspace_id"].startswith("workspace_")


def test_discovery_skips_an_unsafe_marker_and_read_commands_do_not_mutate(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Following a linked marker could inspect a different workspace tree."""
    root = tmp_path / "workspace"
    Workspace.init(root)
    nested = root / "project" / "nested"
    nested.mkdir(parents=True)
    (nested / ".pco").symlink_to(root / ".pco", target_is_directory=True)
    before = _snapshot_tree(root)
    monkeypatch.chdir(nested)

    assert main(["doctor"]) == 0

    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert _snapshot_tree(root) == before


def test_verify_pco_memory_reports_the_read_only_deferred_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Treating the unimplemented projection verifier as successful is misleading."""
    Workspace.init(tmp_path)
    before = _snapshot_tree(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["verify", "--path", "PCO_MEMORY"]) == 1

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["error"] == {
        "code": "WORKSPACE_VERIFICATION_DEFERRED",
        "phase": "verify",
        "message": "PCO_MEMORY verification is deferred until the derivation projection verifier exists",
        "canonical_changed": False,
        "retry_safe": False,
        "recovery": [
            "Use canonical pco search or pco history until derivations are available"
        ],
    }
    assert result["diagnostic"] == {"path": "PCO_MEMORY"}
    assert _snapshot_tree(tmp_path) == before


def test_missing_workspace_preserves_the_structured_error_contract(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A missing workspace must not become a traceback or argparse failure."""
    monkeypatch.chdir(tmp_path)

    assert main(["doctor"]) == 1

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "ok": False,
        "error": {
            "code": "WORKSPACE_NOT_INITIALIZED",
            "phase": "workspace",
            "message": f"Not a PCO workspace above: {tmp_path}",
            "canonical_changed": False,
            "retry_safe": False,
            "recovery": [],
        },
    }


def test_python_module_entrypoint_propagates_main_exit_status(tmp_path: Path) -> None:
    """Discarding ``main``'s return would report a failed command as success."""
    result = subprocess.run(
        [sys.executable, "-m", "pco", "doctor"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "WORKSPACE_NOT_INITIALIZED"
