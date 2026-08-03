from __future__ import annotations

import argparse
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .errors import MemError, ensure
from .models import ApprovalReceipt, Operation, proposal_hash, protected_operations_hash, transaction_fingerprint
from .profile import Profile
from .registry import default_registry
from .repository import MemoryRepository


def _git(root: Path, *args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=text,
        check=False,
    )
    if result.returncode:
        stderr = result.stderr if text else result.stderr.decode("utf-8", errors="replace")
        raise MemError("GIT_COMMAND_FAILED", "pre_commit", stderr.strip())
    return result.stdout


def _materialize_tree(root: Path, revision: str, target: Path) -> None:
    archive = _git(root, "archive", "--format=tar", revision, text=False)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        resolved_target = target.resolve()
        for member in bundle.getmembers():
            if member.issym() or member.islnk() or member.isdev():
                raise MemError("GIT_TREE_LINK_UNSAFE", "pre_commit", member.name)
            candidate = (target / member.name).resolve()
            if candidate != resolved_target and resolved_target not in candidate.parents:
                raise MemError("GIT_TREE_PATH_UNSAFE", "pre_commit", member.name)
        bundle.extractall(target)


def _load_profile(root: Path) -> Profile:
    marker_path = root / ".mem-profile.json"
    if not marker_path.is_file():
        raise MemError("PROFILE_MARKER_NOT_FOUND", "pre_commit", f"Missing {marker_path}")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        raw_profile_path = Path(marker["path"])
    except Exception as exc:
        raise MemError("PROFILE_MARKER_INVALID", "pre_commit", str(exc), path=str(marker_path)) from exc
    profile_path = raw_profile_path if raw_profile_path.is_absolute() else root / raw_profile_path
    profile = Profile.load(profile_path, default_registry())
    if profile.name != marker.get("name") or profile.version != marker.get("version"):
        raise MemError(
            "PROFILE_MARKER_MISMATCH",
            "pre_commit",
            "Canonical Profile does not match .mem-profile.json",
            path=str(marker_path),
        )
    return profile


def _appended_json(old_root: Path, new_root: Path, relative: Path) -> list[dict[str, object]]:
    old = (old_root / relative).read_bytes() if (old_root / relative).exists() else b""
    new = (new_root / relative).read_bytes() if (new_root / relative).exists() else b""
    ensure(new.startswith(old), "APPEND_ONLY_VIOLATION", "pre_commit", f"Historical bytes changed: {relative}", path=str(relative))
    suffix = new[len(old) :]
    if not suffix:
        return []
    ensure(not old or old.endswith(b"\n"), "APPEND_ONLY_VIOLATION", "pre_commit", f"Existing JSONL lacks newline: {relative}")
    try:
        return [json.loads(line) for line in suffix.decode("utf-8").splitlines() if line.strip()]
    except Exception as exc:
        raise MemError("TRANSACTION_DELTA_INVALID", "pre_commit", str(exc), path=str(relative)) from exc


def _verify_increment(root: Path, old_root: Path, staged_root: Path, profile: Profile) -> dict[str, object]:
    actual_by_stream = {
        name: _appended_json(old_root, staged_root, Path(stream.path))
        for name, stream in profile.config.streams.items()
    }
    transaction_path = Path("transactions/transactions.jsonl")
    transaction_records = _appended_json(old_root, staged_root, transaction_path)
    ensure(len(transaction_records) == 1, "TRANSACTION_RECEIPT_REQUIRED", "pre_commit", "A commit must append exactly one transaction receipt")
    transaction_record = transaction_records[0]
    try:
        operations = [Operation.model_validate(item) for item in transaction_record.get("operations", [])]
    except Exception as exc:
        raise MemError("TRANSACTION_RECEIPT_INVALID", "pre_commit", str(exc)) from exc
    ensure(len(operations) == transaction_record.get("operation_count"), "TRANSACTION_RECEIPT_INVALID", "pre_commit", "operation_count does not match operations")
    expected_by_stream: dict[str, list[dict[str, object]]] = {name: [] for name in profile.config.streams}
    expected_paths = {transaction_path.as_posix()}
    for operation in operations:
        if operation.op == "append":
            stream = profile.stream(operation.stream or "")
            ensure(stream.write_policy != "read_only", "STREAM_READ_ONLY", "pre_commit", f"Stream cannot be written: {operation.stream}", stream=operation.stream)
            expected_by_stream[operation.stream or ""].append(operation.record or {})
            expected_paths.add(Path(stream.path).as_posix())
        else:
            assert operation.path is not None and operation.content is not None
            artifact = profile.artifact_path(staged_root, operation.path)
            ensure(artifact.is_file(), "ARTIFACT_MISSING", "pre_commit", operation.path)
            ensure(artifact.read_text(encoding="utf-8") == operation.content, "ARTIFACT_CONTENT_MISMATCH", "pre_commit", operation.path)
            expected_paths.add(Path(operation.path).as_posix())
    for stream, actual in actual_by_stream.items():
        ensure(actual == expected_by_stream[stream], "TRANSACTION_DELTA_MISMATCH", "pre_commit", f"Staged delta is not authorized by transaction operations: {stream}", stream=stream)

    changed = set(str(_git(root, "diff", "--cached", "--name-only", "HEAD")).splitlines())
    ensure(changed == expected_paths, "TRANSACTION_PATH_MISMATCH", "pre_commit", "Staged paths do not exactly match the transaction receipt", value={"changed": sorted(changed), "expected": sorted(expected_paths)})
    head = str(_git(root, "rev-parse", "HEAD")).strip()
    ensure(transaction_record.get("base_commit") == head, "BASE_COMMIT_CHANGED", "pre_commit", "Transaction base is not HEAD")
    ensure(transaction_record.get("profile") == f"{profile.name}@{profile.version}", "PROFILE_VERSION_MISMATCH", "pre_commit", "Transaction uses a different Profile")
    ensure(transaction_record.get("proposal_hash") == proposal_hash(operations), "TRANSACTION_HASH_MISMATCH", "pre_commit", "Proposal hash does not match staged operations")
    expected_fingerprint = transaction_fingerprint(
        base_commit=head,
        profile_name=profile.name,
        profile_version=profile.version,
        fingerprint_context=transaction_record.get("fingerprint_context", {}),
        operations=operations,
    )
    ensure(transaction_record.get("transaction_fingerprint") == expected_fingerprint, "TRANSACTION_HASH_MISMATCH", "pre_commit", "Transaction fingerprint does not match staged operations")
    protected = {
        operation.stream
        for operation in operations
        if operation.op == "append" and operation.stream and profile.stream(operation.stream).write_policy == "user_approval"
    }
    if protected:
        try:
            receipt = ApprovalReceipt.model_validate(transaction_record.get("approval_receipt"))
        except Exception as exc:
            raise MemError("USER_APPROVAL_REQUIRED", "pre_commit", str(exc)) from exc
        reviewed_hash = str(transaction_record.get("fingerprint_context", {}).get("reviewed_proposal_hash") or transaction_record.get("proposal_hash"))
        ensure(receipt.proposal_hash == reviewed_hash, "APPROVAL_STALE", "pre_commit", "Approval reviewed hash does not match")
        ensure(receipt.transaction_proposal_hash == transaction_record.get("proposal_hash"), "APPROVAL_STALE", "pre_commit", "Approval proposal hash does not match")
        ensure(receipt.transaction_fingerprint == expected_fingerprint, "APPROVAL_STALE", "pre_commit", "Approval fingerprint does not match")
        ensure(receipt.protected_operations_hash == protected_operations_hash(operations, protected), "APPROVAL_STALE", "pre_commit", "Protected operation hash does not match")
        for operation in operations:
            if operation.op != "append" or operation.stream not in protected or operation.record is None:
                continue
            pointer = profile.stream(operation.stream).approval_ref_pointer
            value: object = operation.record
            for part in (pointer or "").lstrip("/").split("/"):
                if not part:
                    continue
                key = part.replace("~1", "/").replace("~0", "~")
                ensure(isinstance(value, dict) and key in value, "APPROVAL_REF_MISSING", "pre_commit", f"Missing approval pointer {pointer}")
                value = value[key]
            ensure(value == receipt.id, "APPROVAL_REF_MISMATCH", "pre_commit", "Protected record does not reference receipt")
    else:
        ensure(transaction_record.get("approval_receipt") is None, "APPROVAL_RECEIPT_UNEXPECTED", "pre_commit", "Unprotected transaction includes an approval receipt")
    return {"transaction_id": transaction_record.get("id"), "operations": len(operations)}


def validate_repository(repo_root: Path) -> dict[str, object]:
    root = repo_root.resolve()
    staged_tree = str(_git(root, "write-tree")).strip()
    with tempfile.TemporaryDirectory(prefix="mem-core-hook-") as temporary:
        temporary_root = Path(temporary)
        old_root = temporary_root / "head"
        staged_root = temporary_root / "staged"
        old_root.mkdir()
        staged_root.mkdir()
        _materialize_tree(root, "HEAD", old_root)
        _materialize_tree(root, staged_tree, staged_root)
        profile = _load_profile(staged_root)
        validation = MemoryRepository(staged_root, profile).validate_all(root=staged_root)
        increment = _verify_increment(root, old_root, staged_root, profile)
        return {**validation, "increment": increment}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m mem_core.hook")
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = validate_repository(args.repo)
    except MemError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
