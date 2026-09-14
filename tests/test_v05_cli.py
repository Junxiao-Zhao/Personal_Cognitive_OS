from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from pco import cli as cli_module
from pco.cli import build_parser, main, run
from pco.proposals import archive


def _parser_command_tree(parser: argparse.ArgumentParser) -> dict[str, list[str]]:
    tree: dict[str, list[str]] = {}
    root = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    for command, command_parser in root.choices.items():
        nested = [
            action
            for action in command_parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        tree[command] = sorted(nested[0].choices) if nested else []
    return tree


def _snapshot_tree(root: Path) -> dict[str, tuple[Any, ...]]:
    """Capture the workspace without following links or reading file contents."""

    snapshot: dict[str, tuple[Any, ...]] = {}
    pending = [(root, Path("."))]
    while pending:
        directory, prefix = pending.pop()
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            info = entry.stat(follow_symlinks=False)
            relative = (prefix / entry.name).as_posix()
            snapshot[relative] = (
                stat.S_IFMT(info.st_mode),
                stat.S_IMODE(info.st_mode),
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            if stat.S_ISDIR(info.st_mode):
                pending.append((Path(entry.path), prefix / entry.name))
    return snapshot


def test_public_command_tree_is_exact() -> None:
    """Adding or omitting a public command would violate the clean-break surface."""

    assert _parser_command_tree(build_parser()) == {
        "init": [],
        "doctor": [],
        "verify": [],
        "search": [],
        "history": [],
        "source": ["diff"],
        "proposal": ["abort", "commit", "scaffold", "status", "validate"],
        "derive": ["all"],
    }


def test_parser_accepts_the_complete_public_argument_surface() -> None:
    """Dropping, renaming, or defaulting a documented argument incorrectly breaks Agents."""

    parser = build_parser()
    assert vars(parser.parse_args(["init", "target"])) == {
        "command": "init",
        "path": Path("target"),
    }
    assert vars(parser.parse_args(["verify", "--path", "PCO_MEMORY"])) == {
        "command": "verify",
        "path": Path("PCO_MEMORY"),
    }
    assert vars(
        parser.parse_args(
            [
                "search",
                "pattern",
                "--mode",
                "historical",
                "--limit",
                "17",
                "--start",
                "2026-01-01",
                "--end",
                "2026-08-30T10:00:00Z",
            ]
        )
    ) == {
        "command": "search",
        "query": "pattern",
        "mode": "historical",
        "limit": 17,
        "start": "2026-01-01",
        "end": "2026-08-30T10:00:00Z",
    }
    assert vars(parser.parse_args(["search"])) == {
        "command": "search",
        "query": "",
        "mode": "current",
        "limit": 10,
        "start": None,
        "end": None,
    }
    assert vars(parser.parse_args(["history", "hypotheses", "hyp_1"])) == {
        "command": "history",
        "stream": "hypotheses",
        "record_id": "hyp_1",
    }
    assert vars(parser.parse_args(["source", "diff"])) == {
        "command": "source",
        "source_command": "diff",
        "source_id": None,
    }
    assert vars(parser.parse_args(["source", "diff", "src_1"])) == {
        "command": "source",
        "source_command": "diff",
        "source_id": "src_1",
    }
    for command in ("validate", "commit", "abort"):
        assert vars(parser.parse_args(["proposal", command, "review_example"])) == {
            "command": "proposal",
            "proposal_command": command,
            "review_id": "review_example",
        }
    assert vars(
        parser.parse_args(["derive", "all", "--discard-modified-projection"])
    ) == {
        "command": "derive",
        "derive_command": "all",
        "discard_modified_projection": True,
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor"],
        ["verify"],
        ["search", "pattern"],
        ["source", "diff"],
        ["proposal", "status"],
        ["proposal", "scaffold"],
    ],
)
def test_empty_workspace_read_commands_succeed_without_writes(
    workspace, argv, monkeypatch
) -> None:
    """Dispatching a read command must not prepare or repair workspace state."""

    monkeypatch.chdir(workspace.paths.root)
    before = _snapshot_tree(workspace.paths.root)

    result = run(build_parser().parse_args(argv))

    assert isinstance(result, dict)
    assert result["ok"] is True
    assert _snapshot_tree(workspace.paths.root) == before


def test_history_reads_existing_record_without_writes(
    workspace_with_hypothesis, monkeypatch
) -> None:
    """History dispatch must use canonical records without refreshing derivations."""

    workspace = workspace_with_hypothesis
    monkeypatch.chdir(workspace.paths.root)
    before = _snapshot_tree(workspace.paths.root)

    result = run(build_parser().parse_args(["history", "hypotheses", "hyp_1"]))

    assert result["ok"] is True
    assert result["stream"] == "hypotheses"
    assert result["id"] == "hyp_1"
    assert [record["revision"] for record in result["records"]] == [1]
    assert _snapshot_tree(workspace.paths.root) == before


def test_validate_reads_matching_active_proposal_without_writes(
    workspace_with_proposal, monkeypatch
) -> None:
    """Validation dispatch must inspect but never rewrite a matching proposal."""

    workspace, proposal = workspace_with_proposal
    monkeypatch.chdir(workspace.paths.root)
    before = _snapshot_tree(workspace.paths.root)

    result = run(
        build_parser().parse_args(["proposal", "validate", proposal.review_id])
    )

    assert result["ok"] is True
    assert _snapshot_tree(workspace.paths.root) == before


def test_failed_read_command_via_main_is_also_read_only(
    workspace, monkeypatch, capsys
) -> None:
    """A structured business failure must be nonzero without creating diagnostics files."""

    monkeypatch.chdir(workspace.paths.root)
    before = _snapshot_tree(workspace.paths.root)

    exit_code = main(["proposal", "validate", "review_missing"])

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert exit_code == 1
    assert output["error"]["code"] == "PROPOSAL_NOT_FOUND"
    assert captured.err == ""
    assert _snapshot_tree(workspace.paths.root) == before


def test_derive_all_dispatches_the_explicit_discard_choice(
    workspace, monkeypatch
) -> None:
    """Ignoring the flag would make intentional projection replacement impossible."""

    monkeypatch.chdir(workspace.paths.root)

    def derive(_workspace, *, discard_modified_projection=False):
        return {"ok": True, "discarded": discard_modified_projection}

    monkeypatch.setattr(cli_module, "derive_all", derive, raising=False)

    result = run(
        build_parser().parse_args(["derive", "all", "--discard-modified-projection"])
    )

    assert result == {"ok": True, "discarded": True}


def test_cli_abort_removes_only_the_requested_active_proposal(
    workspace_with_proposal, monkeypatch, capsys
) -> None:
    """Routing abort incorrectly could leave the proposal active or target another ID."""

    workspace, proposal = workspace_with_proposal
    active = workspace.paths.proposals / proposal.review_id
    monkeypatch.chdir(workspace.paths.root)

    assert main(["proposal", "abort", proposal.review_id]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert not active.exists()


def test_cli_commit_exits_success_for_archive_warning(
    workspace_with_proposal, monkeypatch, capsys
) -> None:
    """A non-canonical archive warning must not turn a successful commit into failure."""

    workspace, proposal = workspace_with_proposal
    monkeypatch.chdir(workspace.paths.root)

    def raise_os_error(*_args, **_kwargs):
        raise OSError("injected archive failure")

    monkeypatch.setattr(archive, "atomic_rename", raise_os_error)

    exit_code = main(["proposal", "commit", proposal.review_id])

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert exit_code == 0
    assert result["canonical_status"] == "committed"
    assert result["archive_status"] == "pending"
    assert captured.err == ""


def test_success_prints_one_compact_json_object_and_no_stderr(
    workspace, monkeypatch, capsys
) -> None:
    """Pretty or multi-record output would break line-oriented command consumers."""

    monkeypatch.chdir(workspace.paths.root)

    assert main(["doctor"]) == 0

    captured = capsys.readouterr()
    value = json.loads(captured.out)
    assert (
        captured.out
        == json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )
    assert captured.err == ""


def test_unexpected_failure_prints_one_json_error_without_traceback(
    monkeypatch, capsys
) -> None:
    """An implementation exception must not leak an unstructured traceback."""

    def explode(_args):
        raise RuntimeError("injected unexpected failure")

    monkeypatch.setattr(cli_module, "run", explode)

    assert main(["doctor"]) == 1

    captured = capsys.readouterr()
    value = json.loads(captured.out)
    assert value["error"]["code"] == "UNEXPECTED"
    assert captured.out.count("\n") == 1
    assert captured.err == ""
