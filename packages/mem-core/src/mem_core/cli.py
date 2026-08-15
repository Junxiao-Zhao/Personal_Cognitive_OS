from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import MemError
from .models import Operation
from .profile import Profile
from .registry import default_registry
from .repository import MemoryRepository
from .transaction import TransactionManager


def _input(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "json", None):
        return json.loads(args.json)
    if getattr(args, "input_file", None):
        return json.loads(Path(args.input_file).read_text(encoding="utf-8"))
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            return json.loads(raw)
    return {}


def _common(args: argparse.Namespace) -> tuple[Profile, MemoryRepository, TransactionManager]:
    profile = Profile.load(Path(args.profile), default_registry())
    repository = MemoryRepository(Path(args.repo), profile)
    manager = TransactionManager(repository, Path(args.state))
    return profile, repository, manager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mem")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--profile", required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init")
    commands.add_parser("doctor")

    profile = commands.add_parser("profile")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_commands.add_parser("describe")
    profile_commands.add_parser("validate")
    invoke = profile_commands.add_parser("invoke")
    invoke.add_argument("capability")
    _add_input(invoke)

    record = commands.add_parser("record")
    record_commands = record.add_subparsers(dest="record_command", required=True)
    for name in ("get", "history"):
        target = record_commands.add_parser(name)
        target.add_argument("--stream", required=True)
        target.add_argument("--id", required=True)

    txn = commands.add_parser("txn")
    txn_commands = txn.add_subparsers(dest="txn_command", required=True)
    begin = txn_commands.add_parser("begin")
    begin.add_argument("--id")
    _add_input(begin)
    append = txn_commands.add_parser("append")
    append.add_argument("--id", required=True)
    _add_input(append)
    for name in ("validate", "abort", "status"):
        target = txn_commands.add_parser(name)
        target.add_argument("--id", required=True)
    commit = txn_commands.add_parser("commit")
    commit.add_argument("--id", required=True)
    commit.add_argument("--dry-run", action="store_true")
    approve = txn_commands.add_parser("approve")
    approve.add_argument("--id", required=True)
    _add_input(approve)

    git = commands.add_parser("git")
    git_commands = git.add_subparsers(dest="git_command", required=True)
    git_commands.add_parser("verify")
    return parser


def _add_input(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--json")
    group.add_argument("--input-file")


def run(args: argparse.Namespace) -> dict[str, Any]:
    profile, repository, manager = _common(args)
    if args.command == "init":
        return {"ok": True, "commit": repository.init(), "profile": f"{profile.name}@{profile.version}"}
    if args.command == "doctor":
        return repository.verify()
    if args.command == "profile":
        if args.profile_command == "describe":
            return {"ok": True, "profile": profile.raw, "policy_hash": profile.policy_hash}
        if args.profile_command == "validate":
            return repository.validate_all()
        payload = _input(args)
        result = profile.invoke(args.capability, repo_root=repository.root, **payload)
        return result if isinstance(result, dict) else {"ok": True, "result": result}
    if args.command == "record":
        history = repository.record_history(args.stream, args.id)
        if args.record_command == "history":
            return {"ok": True, "stream": args.stream, "id": args.id, "records": history}
        if not history:
            raise MemError("RECORD_NOT_FOUND", "record", f"Record not found: {args.id}", stream=args.stream, record_id=args.id)
        return {"ok": True, "stream": args.stream, "record": history[-1]}
    if args.command == "txn":
        if args.txn_command == "begin":
            state = manager.begin(fingerprint_context=_input(args), transaction_id=args.id)
            return {"ok": True, "transaction": state.model_dump(mode="json")}
        if args.txn_command == "append":
            state = manager.append(args.id, Operation.model_validate(_input(args)))
            return {"ok": True, "transaction": state.model_dump(mode="json")}
        if args.txn_command == "validate":
            return {"ok": True, "validation": manager.validate(args.id)}
        if args.txn_command == "approve":
            payload = _input(args)
            receipt = manager.attach_approval(
                args.id,
                checkpoint_id=payload["checkpoint_id"],
                proposal_hash_value=payload["proposal_hash"],
                decision_message_id=payload.get("decision_message_id"),
            )
            return {"ok": True, "approval_receipt": receipt.model_dump(mode="json")}
        if args.txn_command == "commit":
            return manager.commit(args.id, dry_run=args.dry_run)
        if args.txn_command == "abort":
            return manager.abort(args.id)
        return manager.status(args.id)
    if args.command == "git" and args.git_command == "verify":
        return repository.verify()
    raise MemError("COMMAND_INVALID", "cli", "Unsupported command")


def main(argv: list[str] | None = None) -> None:
    try:
        result = run(build_parser().parse_args(argv))
    except MemError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(1) from None
    except Exception as exc:
        result = MemError("UNEXPECTED", "cli", str(exc)).as_dict()
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
