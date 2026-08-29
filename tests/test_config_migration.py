from __future__ import annotations

import json
from pathlib import Path

import pytest

from mem_core.errors import MemError
from pco.cli import main
from pco.config import load_config


def _legacy_config(root: Path, *, with_new_fields: bool = False) -> None:
    checkpoint = {"trigger_ratio": 0.5}
    if with_new_fields:
        checkpoint.update(
            {
                "auto_consolidate": {"new_public_tokens": 12000},
                "auto_compact": {"context_ratio": 0.8},
            }
        )
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        "checkpoint:\n" + "".join(f"  {key}: {value}\n" for key, value in checkpoint.items()),
        encoding="utf-8",
    )


def test_legacy_trigger_ratio_is_a_structured_migration_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _legacy_config(workspace)

    with pytest.raises(MemError) as caught:
        load_config(workspace=workspace)

    error = caught.value.as_dict()["error"]
    assert error["code"] == "CONFIG_MIGRATION_REQUIRED"
    assert error["value"]["field"] == "checkpoint.trigger_ratio"
    assert error["value"]["replacement"] == [
        "checkpoint.auto_consolidate.new_public_tokens",
        "checkpoint.auto_compact.context_ratio",
    ]
    assert "consolidate" in " ".join(error["recovery"])


def test_legacy_trigger_ratio_is_never_mapped_even_with_explicit_new_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _legacy_config(workspace, with_new_fields=True)

    with pytest.raises(MemError) as caught:
        load_config(workspace=workspace)

    assert caught.value.as_dict()["error"]["code"] == "CONFIG_MIGRATION_REQUIRED"


def test_cli_emits_machine_detectable_migration_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = tmp_path / "workspace"
    _legacy_config(workspace)

    with pytest.raises(SystemExit) as exited:
        main(["--workspace", str(workspace), "doctor"])

    assert exited.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"]["code"] == "CONFIG_MIGRATION_REQUIRED"
    assert output["error"]["value"]["field"] == "checkpoint.trigger_ratio"
