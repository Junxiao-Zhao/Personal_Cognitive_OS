from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


PLUGIN = Path(__file__).parents[1] / "packages/pco/src/pco/resources/opencode/plugins/pco.ts"
LOOPBACK = Path(__file__).with_name("opencode_question_loopback.ts")


def _opencode_sdk_available() -> bool:
    if shutil.which("bun") is None:
        return False
    probe = subprocess.run(
        ["bun", "--eval", "import('@opencode-ai/plugin').then(() => process.exit(0)).catch(() => process.exit(1))"],
        cwd="/root/.config/opencode",
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


def test_plugin_has_fail_closed_native_question_contract() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    assert 'input.tool !== "question"' in source
    assert 'event.type === "question.asked"' in source
    assert 'event.type === "question.replied"' in source
    assert 'question.dismissed' in source
    assert 'question.closed' in source
    assert 'question.cancelled' in source
    assert 'question.rejected' in source
    assert 'approval_challenge_id' in source
    assert 'question_request_id' in source
    assert 'reason_hash' in source
    assert 'expires_at' in source
    assert '"--question-request-id"' in source
    assert '"--decision-message-id"' not in source
    assert '"pco-yes"' not in source
    assert '"pco-no"' not in source
    assert "custom: true" in source
    assert 'stringField(properties, "id", "requestID"' in source
    assert "scheduleApprovalQuestion" in source
    assert "await scheduleApprovalQuestion(context.sessionID)" in source
    assert "clearQuestion(eventSessionID, eventRequestID)" in source
    assert "!pendingQuestion.questionToolCallID || !eventCallID" in source
    assert "main_evidence" in source

    approve_start = source.index("pco_approve: tool(")
    reject_start = source.index("pco_reject: tool(")
    status_start = source.index("pco_status: tool(")
    assert 'args: {}' in source[approve_start:reject_start]
    assert 'args: {}' in source[reject_start:status_start]
    assert "pendingDecision = undefined" in source[approve_start:status_start]
    for tool_name, next_tool in (
        ("pco_status: tool(", "pco_retry: tool("),
        ("pco_retry: tool(", "pco_abort: tool("),
        ("pco_abort: tool(", "pco_memory_search: tool("),
    ):
        start = source.index(tool_name)
        end = source.index(next_tool, start)
        assert "if (!mainSession(context.sessionID))" in source[start:end]


@pytest.mark.skipif(not _opencode_sdk_available(), reason="Bun/OpenCode SDK is not installed in this environment")
def test_executable_plugin_loopback_is_available(tmp_path: Path) -> None:
    staging = tmp_path / "loopback"
    staging.mkdir()
    shutil.copy2(PLUGIN, staging / "pco.ts")
    fixture = LOOPBACK.read_text(encoding="utf-8").replace(
        "../packages/pco/src/pco/resources/opencode/plugins/pco.ts",
        str(staging / "pco.ts"),
    )
    fixture_path = staging / "loopback.ts"
    fixture_path.write_text(fixture, encoding="utf-8")
    package_root = staging / "node_modules" / "@opencode-ai"
    package_root.mkdir(parents=True)
    package_root.joinpath("plugin").symlink_to("/root/.config/opencode/node_modules/@opencode-ai/plugin")
    environment = os.environ.copy()
    environment["PATH"] = f"{Path(shutil.which('bun')).parent}:{environment.get('PATH', '')}"
    result = subprocess.run(
        ["bun", str(fixture_path)],
        cwd="/root/.config/opencode",
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
