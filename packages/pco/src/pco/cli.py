from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mem_core.errors import MemError, ensure

from .archive import ConversationArchive
from .checkpoint import CheckpointEngine
from .checkpoint.errors import derivation_error, structured_error
from .config import load_config
from .harness import OpenCodeAdapter
from .repo_loader import resolve_derivation_source_commit
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
    add.add_argument("path", type=Path, nargs="?")
    add.add_argument("--locator")
    add.add_argument("--reader")
    add.add_argument("--provider")
    add.add_argument("--name")
    source_commands.add_parser("diff")

    checkpoint = commands.add_parser("checkpoint")
    checkpoint_commands = checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    request = checkpoint_commands.add_parser("request")
    request.add_argument("--trigger", choices=["manual", "auto"], default="manual")
    request.add_argument("--intent", choices=["consolidate", "compact"], default="compact")
    request.add_argument("--origin", choices=["command", "idle_threshold", "harness_auto_compaction"])
    # Private Host-to-Adapter fields. They are intentionally not exposed by
    # the pco_checkpoint tool; the Plugin supplies them only for compact.
    request.add_argument("--native-compact-token")
    request.add_argument("--native-compact-attempt-id")
    request.add_argument("--native-compact-expires-at", type=int)
    request.add_argument("--pending-compaction-json")
    checkpoint_commands.add_parser("auto-if-needed")
    checkpoint_commands.add_parser("auto-probe")
    decide = checkpoint_commands.add_parser("decide")
    decide.add_argument("--decision", choices=["yes", "no"], required=True)
    decide.add_argument("--reason")
    decide.add_argument("--question-request-id")
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
    native_compact_bypass = None
    native_token = getattr(args, "native_compact_token", None)
    native_attempt = getattr(args, "native_compact_attempt_id", None)
    native_expires = getattr(args, "native_compact_expires_at", None)
    if native_token is not None or native_attempt is not None or native_expires is not None:
        ensure(
            isinstance(native_token, str) and native_token
            and isinstance(native_attempt, str) and native_attempt
            and isinstance(native_expires, int) and native_expires > 0,
            "NATIVE_COMPACT_TOKEN_INVALID",
            "cli",
            "Native compact token fields must be supplied together",
        )
        native_compact_bypass = {
            "token": native_token,
            "checkpoint_id": "pending",
            "session_id": args.session_id or binding.native_session_id,
            "attempt_id": native_attempt,
            "expires_at": native_expires,
        }
    return OpenCodeAdapter(
        base_url=args.server_url or workspace.config.harness.base_url,
        directory=Path.cwd(),
        state_root=workspace.config.state_root,
        session_id=args.session_id or binding.native_session_id,
        model_context_tokens=workspace.config.harness.model_context_tokens,
        timeout=workspace.config.harness.request_timeout_seconds,
        native_compact_bypass=native_compact_bypass,
    )


def _native_compact_bypass(args: argparse.Namespace) -> dict[str, Any] | None:
    token = getattr(args, "native_compact_token", None)
    attempt = getattr(args, "native_compact_attempt_id", None)
    expires = getattr(args, "native_compact_expires_at", None)
    if token is None and attempt is None and expires is None:
        return None
    ensure(
        isinstance(token, str) and token
        and isinstance(attempt, str) and attempt
        and isinstance(expires, int) and expires > 0,
        "NATIVE_COMPACT_TOKEN_INVALID",
        "cli",
        "Native compact token fields must be supplied together",
    )
    return {
        "token": token,
        "checkpoint_id": "pending",
        "session_id": args.session_id,
        "attempt_id": attempt,
        "expires_at": expires,
    }


def _pending_compaction(args: argparse.Namespace) -> dict[str, Any] | None:
    raw = getattr(args, "pending_compaction_json", None)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MemError(
            "PENDING_COMPACTION_INVALID",
            "checkpoint",
            "Pending compaction payload must be valid JSON",
            recovery=["Retry with the durable pending-compaction payload intact"],
        ) from exc
    ensure(
        isinstance(value, dict),
        "PENDING_COMPACTION_INVALID",
        "checkpoint",
        "Pending compaction payload must be an object",
    )
    # The Plugin uses snake_case on the process boundary. Accept the legacy
    # camelCase spelling too so a request written by an older Plugin can be
    # imported and then normalized by Pydantic's strict model.
    aliases = {
        "requestID": "request_id",
        "eventID": "event_id",
        "sessionID": "session_id",
        "requestedBoundary": "requested_boundary",
        "requestedAt": "requested_at",
    }
    return {aliases.get(key, key): item for key, item in value.items()}


def _derivation_error_as_memerror(error: dict[str, Any]) -> MemError:
    details = {key: error[key] for key in ("stream", "record_id", "path", "value") if key in error}
    return MemError(
        str(error["code"]),
        str(error["phase"]),
        str(error["message"]),
        retryable=bool(error.get("retryable", True)),
        recovery=list(error.get("recovery", [])),
        **details,
    )


def _invoke_derivation(workspace: Workspace, capability: str, phase: str, **kwargs: Any) -> dict[str, Any]:
    try:
        result = workspace.profile.invoke(capability, **kwargs)
    except MemError:
        raise
    except Exception as exc:
        raise _derivation_error_as_memerror(derivation_error(exc, phase)) from exc
    if isinstance(result, dict) and result.get("ok") is False:
        raise _derivation_error_as_memerror(structured_error(result.get("error") or result, phase))
    return result


def _install_opencode(workspace: Workspace, project: Path, *, force: bool) -> dict[str, Any]:
    from importlib.resources import files

    source_root = Path(str(files("pco.resources.opencode")))
    target_root = project.resolve() / ".opencode"
    manifest_path = target_root / ".pco-managed.json"
    legacy_paths = {"commands/pco-yes.md", "commands/pco-no.md"}

    def digest(data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()

    previous_files: dict[str, str] = {}
    if manifest_path.is_file():
        try:
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw_files = raw_manifest.get("files", {})
            if isinstance(raw_files, dict):
                previous_files = {str(path): str(value) for path, value in raw_files.items()}
        except (OSError, TypeError, ValueError):
            # A malformed manifest cannot authorize deletion. Normal file
            # conflict handling below remains the safe fallback.
            previous_files = {}

    source_files = {
        source.relative_to(source_root).as_posix(): source
        for source in source_root.rglob("*")
        if source.is_file() and source.name != "__init__.py" and "__pycache__" not in source.parts
    }
    installed: list[str] = []
    updated: list[str] = []
    removed: list[str] = []

    # A managed manifest is an authorization to remove files only inside the
    # current .opencode tree. Reject malformed or escaping entries before any
    # filesystem mutation; a compromised manifest must never become an
    # arbitrary-file deletion primitive.
    for relative_name in previous_files:
        relative_path = Path(relative_name)
        candidate = (target_root / relative_path).resolve()
        if (
            not relative_name
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or not candidate.is_relative_to(target_root)
        ):
            raise MemError(
                "OPENCODE_MANIFEST_INVALID",
                "install",
                f"Managed manifest contains a path outside .opencode: {relative_name!r}",
                path=relative_name,
                recovery=["Remove or repair .opencode/.pco-managed.json and rerun installation"],
            )

    # Complete every overwrite/conflict check before deleting legacy files or
    # stale manifest entries. A failed install must leave the prior install
    # intact, especially when a user-owned file conflicts with a package file.
    source_bytes: dict[str, bytes] = {}
    changed_files: set[str] = set()
    for relative_name, source in sorted(source_files.items()):
        data = source.read_bytes()
        source_bytes[relative_name] = data
        target = target_root / relative_name
        changed = target.exists() and target.read_bytes() != data
        if changed:
            changed_files.add(relative_name)
        if changed and not force:
            raise MemError(
                "OPENCODE_INTEGRATION_CONFLICT",
                "install",
                f"Refusing to overwrite an existing OpenCode file: {target}",
                path=str(target),
                recovery=["Review the file and rerun with --force if replacement is intended"],
            )

    # Explicit migration for the two PCO commands removed in this release.
    # No directory scan is used, so unrelated user commands are untouched.
    for relative_name in sorted(legacy_paths):
        target = target_root / relative_name
        if target.is_file():
            previous_hash = previous_files.get(relative_name)
            if previous_hash is None or digest(target.read_bytes()) == previous_hash:
                target.unlink()
                removed.append(str(target))

    # Future upgrades may remove another PCO-managed file. Only delete a
    # previous manifest entry when its on-disk bytes still match our hash.
    for relative_name, previous_hash in previous_files.items():
        if relative_name in source_files or relative_name in legacy_paths:
            continue
        target = target_root / relative_name
        if target.is_file() and digest(target.read_bytes()) == previous_hash:
            target.unlink()
            removed.append(str(target))

    for relative_name, source in sorted(source_files.items()):
        target = target_root / relative_name
        data = source_bytes[relative_name]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        installed.append(str(target))
        if relative_name in changed_files:
            updated.append(str(target))

    manifest = {
        "version": 1,
        "files": {relative_name: digest(data) for relative_name, data in sorted(source_bytes.items())},
    }
    target_root.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "project": str(project.resolve()),
        "workspace": str(workspace.root),
        "installed": installed,
        "updated": updated,
        "removed_legacy": removed,
    }


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
            ensure(
                not (args.path and args.locator),
                "SOURCE_INPUT_CONFLICT",
                "cli",
                "source add accepts either a local path or --locator, not both",
                recovery=["Use pco source add PATH", "Use pco source add --locator LOCATOR --reader READER"],
            )
            if args.path is not None:
                ensure(args.reader is None, "SOURCE_INPUT_CONFLICT", "cli", "--reader is only valid with --locator")
                ensure(args.provider is None, "SOURCE_INPUT_CONFLICT", "cli", "--provider is only valid with --locator")
                return manager.register_local(args.path, display_name=args.name)
            ensure(args.locator, "SOURCE_LOCATOR_REQUIRED", "cli", "Remote source add requires --locator")
            ensure(args.reader, "SOURCE_READER_REQUIRED", "cli", "Remote source add requires --reader")
            return manager.register_locator(
                args.locator,
                reader_skill=args.reader,
                provider=args.provider or "remote",
                display_name=args.name,
            )
        result = manager.collect_diffs()
        return {"ok": True, "changes": result["changes"], "source_hashes": result["source_hashes"]}
    if args.command == "checkpoint":
        engine = CheckpointEngine(workspace, adapter)
        if args.checkpoint_command == "request":
            return engine.request(
                args.trigger,
                args.intent,
                args.origin,
                pending_compaction=_pending_compaction(args),
                native_compact_bypass=_native_compact_bypass(args),
            )
        if args.checkpoint_command == "auto-if-needed":
            intent = "compact" if engine.should_auto_checkpoint("compact") else "consolidate" if engine.should_auto_checkpoint("consolidate") else None
            if intent is None:
                return {"ok": True, "triggered": False, "context_usage": adapter.estimate_context_usage()}
            return {"triggered": True, "intent": intent, **engine.request("auto", intent)}
        if args.checkpoint_command == "auto-probe":
            compact_needed = engine.should_auto_checkpoint("compact")
            consolidate_needed = engine.should_auto_checkpoint("consolidate")
            return {
                "ok": True,
                "needed": compact_needed or consolidate_needed,
                "intent": "compact" if compact_needed else "consolidate" if consolidate_needed else None,
                "context_usage": adapter.estimate_context_usage(),
            }
        if args.checkpoint_command == "decide":
            ensure(args.approval_grant, "APPROVAL_GRANT_REQUIRED", "approval", "Approval must come from the host-issued interaction")
            ensure(args.question_request_id, "QUESTION_REQUEST_ID_REQUIRED", "approval", "A native question request ID is required for the decision")
            return engine.decide(
                args.decision,
                reason=args.reason,
                question_request_id=args.question_request_id,
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
        source_commit = resolve_derivation_source_commit(
            workspace.config.memory_root,
            state_root=workspace.config.state_root,
        )
        return workspace.profile.invoke(
            "retrieval.search",
            repo_root=workspace.config.memory_root,
            query=args.query,
            mode=args.mode,
            limit=args.limit,
            start=args.start,
            end=args.end,
            indexes_root=workspace.config.indexes_root,
            source_commit=source_commit,
        )
    if args.command == "derive":
        source_commit = resolve_derivation_source_commit(
            workspace.config.memory_root,
            state_root=workspace.config.state_root,
        )
        if args.derive_command == "index":
            return _invoke_derivation(
                workspace,
                "index.build",
                "index",
                repo_root=workspace.config.memory_root,
                indexes_root=workspace.config.indexes_root,
                force=args.force,
                source_commit=source_commit,
            )
        if args.derive_command == "backlinks":
            return _invoke_derivation(
                workspace,
                "backlinks.build",
                "backlinks",
                repo_root=workspace.config.memory_root,
                output_path=workspace.config.state_root / "derivations" / "backlinks.json",
                source_commit=source_commit,
            )
        if args.derive_command == "context":
            return _invoke_derivation(
                workspace,
                "context_renderer.render",
                "context",
                repo_root=workspace.config.memory_root,
                output_path=workspace.config.state_root / "context" / "current.md",
                source_commit=source_commit,
            )
        target = args.target
        kwargs = {
            "repo_root": workspace.config.memory_root,
            "source_commit": source_commit,
        }
        if target == "markdown":
            kwargs["output_root"] = workspace.config.projection_root
        else:
            kwargs["state_root"] = workspace.config.state_root
        return _invoke_derivation(workspace, f"projections.{target}", "projection", **kwargs)
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
