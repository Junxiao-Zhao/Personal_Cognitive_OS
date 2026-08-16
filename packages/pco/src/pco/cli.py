from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mem_core.errors import MemError, ensure

from .archive import ConversationArchive
from .backlinks import build as build_backlinks
from .checkpoint import CheckpointEngine
from .config import load_config
from .context import render as render_context
from .harness import OpenCodeAdapter
from .projections import project_affine, project_markdown
from .retrieval import build_index, search
from .sources import SourceManager
from .workspace import Workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pco")
    parser.add_argument("--workspace", type=Path, default=Path(".pco"))
    parser.add_argument("--server-url")
    parser.add_argument("--session-id")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--projection", choices=["affine", "markdown", "none"], default="affine")
    commands.add_parser("doctor")
    run = commands.add_parser("run")
    run.add_argument("--project", type=Path, default=Path.cwd())
    run.add_argument("--model")
    run.add_argument("--continue-session", action="store_true")
    install = commands.add_parser("install-opencode")
    install.add_argument("--project", type=Path, default=Path.cwd())
    install.add_argument("--force", action="store_true")
    commands.add_parser("sync")

    source = commands.add_parser("source")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    add = source_commands.add_parser("add")
    add.add_argument("path", type=Path)
    add.add_argument("--name")
    source_commands.add_parser("diff")

    checkpoint = commands.add_parser("checkpoint")
    checkpoint_commands = checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    request = checkpoint_commands.add_parser("request")
    request.add_argument("--trigger", choices=["manual", "auto"], default="manual")
    checkpoint_commands.add_parser("auto-if-needed")
    checkpoint_commands.add_parser("auto-probe")
    decide = checkpoint_commands.add_parser("decide")
    decide.add_argument("--decision", choices=["yes", "no"], required=True)
    decide.add_argument("--reason")
    decide.add_argument("--decision-message-id")
    decide.add_argument("--approval-grant")
    checkpoint_commands.add_parser("status")
    checkpoint_commands.add_parser("retry")
    checkpoint_commands.add_parser("abort")
    checkpoint_commands.add_parser("retry-derivations")

    retrieval = commands.add_parser("search")
    retrieval.add_argument("query")
    retrieval.add_argument("--mode", choices=["continuity", "current", "pattern", "historical", "change"], default="current")
    retrieval.add_argument("--limit", type=int, default=10)
    retrieval.add_argument("--start")
    retrieval.add_argument("--end")
    derive = commands.add_parser("derive")
    derive_commands = derive.add_subparsers(dest="derive_command", required=True)
    index = derive_commands.add_parser("index")
    index.add_argument("--force", action="store_true")
    derive_commands.add_parser("backlinks")
    projection = derive_commands.add_parser("projection")
    projection.add_argument("--target", choices=["affine", "markdown"], required=True)
    derive_commands.add_parser("context")
    return parser


def _workspace(args: argparse.Namespace, *, init: bool = False) -> Workspace:
    overrides = []
    if init:
        overrides.append(f"checkpoint.derivations.projection={args.projection}")
    config = load_config(workspace=args.workspace, overrides=overrides)
    workspace = Workspace(config)
    if not init:
        workspace.assert_initialized()
        workspace.refresh_repository_profile()
    return workspace


def _adapter(workspace: Workspace, args: argparse.Namespace) -> OpenCodeAdapter:
    binding = workspace.binding()
    return OpenCodeAdapter(
        base_url=args.server_url or workspace.config.harness.base_url,
        directory=Path.cwd(),
        state_root=workspace.config.state_root,
        session_id=args.session_id or binding.native_session_id,
        model_context_tokens=workspace.config.harness.model_context_tokens,
        timeout=workspace.config.harness.request_timeout_seconds,
    )


def _install_opencode(workspace: Workspace, project: Path, *, force: bool) -> dict[str, Any]:
    from importlib.resources import files

    source_root = Path(str(files("pco.resources.opencode")))
    target_root = project.resolve() / ".opencode"
    installed: list[str] = []
    for source in source_root.rglob("*"):
        if not source.is_file() or source.name == "__init__.py" or "__pycache__" in source.parts:
            continue
        relative = source.relative_to(source_root)
        target = target_root / relative
        if target.exists() and target.read_bytes() != source.read_bytes() and not force:
            raise MemError(
                "OPENCODE_INTEGRATION_CONFLICT",
                "install",
                f"Refusing to overwrite an existing OpenCode file: {target}",
                path=str(target),
                recovery=["Review the file and rerun with --force if replacement is intended"],
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        installed.append(str(target))
    return {"ok": True, "project": str(project.resolve()), "workspace": str(workspace.root), "installed": installed}


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "init":
        return _workspace(args, init=True).init()
    workspace = _workspace(args)
    if args.command == "doctor":
        return workspace.doctor()
    if args.command == "install-opencode":
        return _install_opencode(workspace, args.project, force=args.force)
    if args.command == "run":
        _install_opencode(workspace, args.project, force=False)
        parsed = urlparse(args.server_url or workspace.config.harness.base_url)
        command = ["opencode", str(args.project.resolve()), "--hostname", parsed.hostname or "127.0.0.1", "--port", str(parsed.port or 4096)]
        binding = workspace.binding()
        if binding.native_session_id:
            command.extend(["--session", binding.native_session_id])
        elif args.continue_session:
            command.append("--continue")
        if args.model:
            command.extend(["--model", args.model])
        environment = os.environ.copy()
        environment["PCO_WORKSPACE"] = str(workspace.root)
        os.execvpe(command[0], command, environment)
        raise AssertionError("unreachable")
    adapter = _adapter(workspace, args)
    if args.command == "sync":
        session = adapter.attach_or_create()
        binding = workspace.binding()
        if binding.native_session_id is None:
            binding.native_session_id = session
            workspace.save_binding(binding)
        thread = workspace.thread()
        messages = adapter.archive_messages_since(thread.archive_cursor)
        return ConversationArchive(workspace).archive(messages)
    if args.command == "source":
        manager = SourceManager(workspace)
        if args.source_command == "add":
            return manager.register_local(args.path, display_name=args.name)
        result = manager.collect_diffs()
        return {"ok": True, "changes": result["changes"], "source_hashes": result["source_hashes"]}
    if args.command == "checkpoint":
        engine = CheckpointEngine(workspace, adapter)
        if args.checkpoint_command == "request":
            return engine.request(args.trigger)
        if args.checkpoint_command == "auto-if-needed":
            if not engine.should_auto_checkpoint():
                return {"ok": True, "triggered": False, "context_usage": adapter.estimate_context_usage()}
            return {"triggered": True, **engine.request("auto")}
        if args.checkpoint_command == "auto-probe":
            return {"ok": True, "needed": engine.should_auto_checkpoint(), "context_usage": adapter.estimate_context_usage()}
        if args.checkpoint_command == "decide":
            if args.decision == "yes":
                ensure(args.approval_grant, "APPROVAL_GRANT_REQUIRED", "approval", "Approval must come from the host-issued approval interaction")
            return engine.decide(
                args.decision,
                reason=args.reason,
                native_message_id=args.decision_message_id,
                approval_grant=args.approval_grant,
                session_id=args.session_id,
            )
        if args.checkpoint_command == "status":
            return engine.status()
        if args.checkpoint_command == "retry":
            return engine.retry()
        if args.checkpoint_command == "retry-derivations":
            return engine.retry_derivations()
        return engine.abort()
    if args.command == "search":
        return search(repo_root=workspace.config.memory_root, query=args.query, mode=args.mode, limit=args.limit, start=args.start, end=args.end)
    if args.command == "derive":
        if args.derive_command == "index":
            return build_index(repo_root=workspace.config.memory_root, indexes_root=workspace.config.indexes_root, force=args.force)
        if args.derive_command == "backlinks":
            return build_backlinks(repo_root=workspace.config.memory_root, output_path=workspace.config.state_root / "derivations" / "backlinks.json")
        if args.derive_command == "context":
            return render_context(repo_root=workspace.config.memory_root, output_path=workspace.config.state_root / "context" / "current.md")
        if args.target == "markdown":
            return project_markdown(repo_root=workspace.config.memory_root, output_root=workspace.config.projection_root)
        return project_affine(repo_root=workspace.config.memory_root, state_root=workspace.config.state_root)
    raise MemError("COMMAND_INVALID", "cli", "Unsupported command")


def main(argv: list[str] | None = None) -> None:
    try:
        result = run(build_parser().parse_args(argv))
    except MemError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(1) from None
    except ValueError as exc:
        error = MemError("INVALID_INPUT", "cli", str(exc)).as_dict()
        print(json.dumps(error, ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(1) from None
    except Exception as exc:
        error = MemError("UNEXPECTED", "cli", str(exc), retryable=True).as_dict()
        print(json.dumps(error, ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
