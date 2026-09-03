from __future__ import annotations

import argparse
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .approval import verify_approval_receipt
from .delta import is_fast_path, validate_delta_records, validate_structured_delta
from .errors import MemError, ensure
from .models import (
    ApprovalReceipt,
    Operation,
    RecordEnvelope,
    proposal_hash,
    transaction_fingerprint,
)
from .profile import Profile
from .profile_identity import snapshot_profile_tree
from .registry import default_registry


def _git(root: Path, *args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=text,
        check=False,
    )
    if result.returncode:
        stderr = (
            result.stderr if text else result.stderr.decode("utf-8", errors="replace")
        )
        raise MemError(
            "GIT_COMMAND_FAILED",
            "pre_commit",
            stderr.strip(),
            canonical_changed=False,
            retry_safe=False,
        )
    return result.stdout


def _has_head(root: Path) -> bool:
    try:
        _git(root, "rev-parse", "--verify", "HEAD")
    except MemError:
        return False
    return True


def _old_bytes(root: Path, relative: str) -> bytes:
    """Read a path's bytes at HEAD without materializing the whole tree."""
    try:
        return _git(root, "show", f"HEAD:{relative}", text=False)
    except MemError:
        return b""


def _staged_bytes(root: Path, relative: str) -> bytes:
    """Read a path's bytes from the index (what pre-commit must validate)."""
    try:
        return _git(root, "show", f":{relative}", text=False)
    except MemError:
        return b""


def _root_bytes(root: Path, relative: str) -> bytes:
    """Read a path from the repository root commit, if it existed there."""

    root_commit = str(_git(root, "rev-list", "--max-parents=0", "HEAD")).strip()
    try:
        return _git(root, "show", f"{root_commit}:{relative}", text=False)
    except MemError:
        return b""


def _tree_bytes(root: Path, revision: str, relative: str) -> bytes:
    try:
        return _git(root, "show", f"{revision}:{relative}", text=False)
    except MemError:
        return b""


def _extract_archive(
    root: Path, revision: str, target: Path, paths: list[str] | None = None
) -> None:
    command = ["archive", "--format=tar", revision]
    if paths:
        command.extend(["--", *paths])
    archive = _git(root, *command, text=False)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        resolved_target = target.resolve()
        for member in bundle.getmembers():
            if member.issym() or member.islnk() or member.isdev():
                raise MemError(
                    "GIT_TREE_LINK_UNSAFE",
                    "pre_commit",
                    member.name,
                    canonical_changed=False,
                    retry_safe=False,
                )
            candidate = (target / member.name).resolve()
            if (
                candidate != resolved_target
                and resolved_target not in candidate.parents
            ):
                raise MemError(
                    "GIT_TREE_PATH_UNSAFE",
                    "pre_commit",
                    member.name,
                    canonical_changed=False,
                    retry_safe=False,
                )
        try:
            bundle.extractall(target, filter="data")
        except TypeError:
            bundle.extractall(target)


def _trusted_relative_path(raw_path: str, *, phase: str) -> Path:
    relative = Path(raw_path)
    ensure(
        not relative.is_absolute(),
        "PROFILE_PATH_UNSAFE",
        phase,
        "Profile paths must be relative",
        value=raw_path,
        canonical_changed=False,
        retry_safe=False,
    )
    ensure(
        ".." not in relative.parts,
        "PROFILE_PATH_UNSAFE",
        phase,
        "Profile paths cannot escape their root",
        value=raw_path,
        canonical_changed=False,
        retry_safe=False,
    )
    return relative


def _marker_string(marker: dict[str, object], key: str) -> str:
    value = marker.get(key)
    ensure(
        isinstance(value, str) and bool(value),
        "PROFILE_MARKER_INVALID",
        "pre_commit",
        f".mem-profile.json field must be a non-empty string: {key}",
        path=".mem-profile.json",
        value={key: value},
        canonical_changed=False,
        retry_safe=False,
    )
    return value


def _load_external_profile(marker: dict[str, object], target: Path) -> Profile:
    profile_path = Path(_marker_string(marker, "path"))
    ensure(
        profile_path.is_absolute(),
        "PROFILE_PATH_UNSAFE",
        "pre_commit",
        "External Profile paths must be absolute",
        value=str(profile_path),
        canonical_changed=False,
        retry_safe=False,
    )
    expected_digest = _marker_string(marker, "profile_tree_digest")
    expected_policy_hash = _marker_string(marker, "policy_hash")
    target.mkdir(parents=True, exist_ok=True)
    actual_digest = snapshot_profile_tree(profile_path, target, phase="pre_commit")
    ensure(
        actual_digest == expected_digest,
        "PROFILE_TREE_MISMATCH",
        "pre_commit",
        "External Profile tree does not match .mem-profile.json",
        path=str(profile_path),
        value={"expected": expected_digest, "actual": actual_digest},
        canonical_changed=False,
        retry_safe=False,
    )
    profile = Profile.load(target, default_registry())
    ensure(
        profile.policy_hash == expected_policy_hash,
        "PROFILE_POLICY_MISMATCH",
        "pre_commit",
        "External Profile policy does not match .mem-profile.json",
        path=str(profile_path),
        value={
            "expected": expected_policy_hash,
            "actual": profile.policy_hash,
        },
        canonical_changed=False,
        retry_safe=False,
    )
    return profile


def _load_profile_from_tree(root: Path, revision: str, target: Path) -> Profile:
    marker_bytes = _tree_bytes(root, revision, ".mem-profile.json")
    if not marker_bytes:
        raise MemError(
            "PROFILE_MARKER_NOT_FOUND",
            "pre_commit",
            "Missing .mem-profile.json",
            canonical_changed=False,
            retry_safe=False,
        )
    try:
        marker = json.loads(marker_bytes.decode("utf-8"))
    except Exception as exc:
        raise MemError(
            "PROFILE_MARKER_INVALID",
            "pre_commit",
            str(exc),
            path=".mem-profile.json",
            canonical_changed=False,
            retry_safe=False,
        ) from exc
    ensure(
        isinstance(marker, dict),
        "PROFILE_MARKER_INVALID",
        "pre_commit",
        ".mem-profile.json must contain a JSON object",
        path=".mem-profile.json",
        canonical_changed=False,
        retry_safe=False,
    )
    raw_path = _marker_string(marker, "path")
    if Path(raw_path).is_absolute():
        profile = _load_external_profile(marker, target)
        if profile.name != marker.get("name") or profile.version != marker.get(
            "version"
        ):
            raise MemError(
                "PROFILE_MARKER_MISMATCH",
                "pre_commit",
                "Canonical Profile does not match .mem-profile.json",
                path=".mem-profile.json",
                canonical_changed=False,
                retry_safe=False,
            )
        return profile
    profile_relative = _trusted_relative_path(raw_path, phase="pre_commit")
    target.mkdir(parents=True, exist_ok=True)
    _extract_archive(
        root,
        revision,
        target,
        [".mem-profile.json", profile_relative.as_posix()],
    )
    profile_path = target / profile_relative
    profile = Profile.load(profile_path, default_registry())
    if profile.name != marker.get("name") or profile.version != marker.get("version"):
        raise MemError(
            "PROFILE_MARKER_MISMATCH",
            "pre_commit",
            "Canonical Profile does not match .mem-profile.json",
            path=".mem-profile.json",
            canonical_changed=False,
            retry_safe=False,
        )
    return profile


def _appended_json(old: bytes, new: bytes, relative: Path) -> list[dict[str, object]]:
    ensure(
        new.startswith(old),
        "APPEND_ONLY_VIOLATION",
        "pre_commit",
        f"Historical bytes changed: {relative}",
        path=str(relative),
        canonical_changed=False,
        retry_safe=False,
    )
    suffix = new[len(old) :]
    if not suffix:
        return []
    ensure(
        not old or old.endswith(b"\n"),
        "APPEND_ONLY_VIOLATION",
        "pre_commit",
        f"Existing JSONL lacks newline: {relative}",
        canonical_changed=False,
        retry_safe=False,
    )
    try:
        return [
            json.loads(line)
            for line in suffix.decode("utf-8").splitlines()
            if line.strip()
        ]
    except Exception as exc:
        raise MemError(
            "TRANSACTION_DELTA_INVALID",
            "pre_commit",
            str(exc),
            path=str(relative),
            canonical_changed=False,
            retry_safe=False,
        ) from exc


def _verify_increment(
    root: Path,
    profile: Profile,
    changed: set[str],
    bytes_by_stream: dict[str, tuple[bytes, bytes]],
    transaction_bytes: tuple[bytes, bytes],
) -> dict[str, object]:
    actual_by_stream: dict[str, list[dict[str, object]]] = {}
    for name, stream in profile.config.streams.items():
        relative = Path(stream.path)
        old, new = bytes_by_stream.get(name, (b"", b""))
        actual_by_stream[name] = _appended_json(old, new, relative)
        if actual_by_stream[name] and profile.uses_delta_fast_path(name):
            # Preserve the cheap, useful error for malformed fast-path
            # records even when a caller bypasses the transaction receipt.
            # The same policy is applied later to the authorized delta.
            validate_delta_records(profile, {name: actual_by_stream[name]})
    transaction_path = Path("transactions/transactions.jsonl")
    transaction_records = _appended_json(
        transaction_bytes[0], transaction_bytes[1], transaction_path
    )
    ensure(
        len(transaction_records) == 1,
        "TRANSACTION_RECEIPT_REQUIRED",
        "pre_commit",
        "A commit must append exactly one transaction receipt",
        canonical_changed=False,
        retry_safe=False,
    )
    transaction_record = transaction_records[0]
    try:
        operations = [
            Operation.model_validate(item)
            for item in transaction_record.get("operations", [])
        ]
    except Exception as exc:
        raise MemError(
            "TRANSACTION_RECEIPT_INVALID",
            "pre_commit",
            str(exc),
            canonical_changed=False,
            retry_safe=False,
        ) from exc
    ensure(
        len(operations) == transaction_record.get("operation_count"),
        "TRANSACTION_RECEIPT_INVALID",
        "pre_commit",
        "operation_count does not match operations",
        canonical_changed=False,
        retry_safe=False,
    )
    expected_by_stream: dict[str, list[dict[str, object]]] = {
        name: [] for name in profile.config.streams
    }
    expected_paths = {transaction_path.as_posix()}
    for operation in operations:
        if operation.op == "append":
            stream = profile.stream(operation.stream or "")
            ensure(
                stream.write_policy != "read_only",
                "STREAM_READ_ONLY",
                "pre_commit",
                f"Stream cannot be written: {operation.stream}",
                stream=operation.stream,
                canonical_changed=False,
                retry_safe=False,
            )
            expected_by_stream[operation.stream or ""].append(operation.record or {})
            expected_paths.add(Path(stream.path).as_posix())
        else:
            assert operation.path is not None and operation.content is not None
            profile.artifact_path(root, operation.path)
            ensure(
                operation.path in changed,
                "ARTIFACT_MISSING",
                "pre_commit",
                operation.path,
                canonical_changed=False,
                retry_safe=False,
            )
            artifact_content = _staged_bytes(root, operation.path).decode("utf-8")
            ensure(
                artifact_content == operation.content,
                "ARTIFACT_CONTENT_MISMATCH",
                "pre_commit",
                operation.path,
                canonical_changed=False,
                retry_safe=False,
            )
            expected_paths.add(Path(operation.path).as_posix())
    for stream, actual in actual_by_stream.items():
        ensure(
            actual == expected_by_stream[stream],
            "TRANSACTION_DELTA_MISMATCH",
            "pre_commit",
            f"Staged delta is not authorized by transaction operations: {stream}",
            stream=stream,
            canonical_changed=False,
            retry_safe=False,
        )

    ensure(
        changed == expected_paths,
        "TRANSACTION_PATH_MISMATCH",
        "pre_commit",
        "Staged paths do not exactly match the transaction receipt",
        value={"changed": sorted(changed), "expected": sorted(expected_paths)},
        canonical_changed=False,
        retry_safe=False,
    )
    head = str(_git(root, "rev-parse", "HEAD")).strip()
    ensure(
        transaction_record.get("base_commit") == head,
        "BASE_COMMIT_CHANGED",
        "pre_commit",
        "Transaction base is not HEAD",
        canonical_changed=False,
        retry_safe=False,
    )
    ensure(
        transaction_record.get("profile") == f"{profile.name}@{profile.version}",
        "PROFILE_VERSION_MISMATCH",
        "pre_commit",
        "Transaction uses a different Profile",
        canonical_changed=False,
        retry_safe=False,
    )
    ensure(
        transaction_record.get("proposal_hash") == proposal_hash(operations),
        "TRANSACTION_HASH_MISMATCH",
        "pre_commit",
        "Proposal hash does not match staged operations",
        canonical_changed=False,
        retry_safe=False,
    )
    expected_fingerprint = transaction_fingerprint(
        base_commit=head,
        profile_name=profile.name,
        profile_version=profile.version,
        fingerprint_context=transaction_record.get("fingerprint_context", {}),
        operations=operations,
    )
    ensure(
        transaction_record.get("transaction_fingerprint") == expected_fingerprint,
        "TRANSACTION_HASH_MISMATCH",
        "pre_commit",
        "Transaction fingerprint does not match staged operations",
        canonical_changed=False,
        retry_safe=False,
    )
    protected = {
        operation.stream
        for operation in operations
        if operation.op == "append"
        and operation.stream
        and profile.stream(operation.stream).write_policy == "user_approval"
    }
    if protected:
        try:
            receipt = ApprovalReceipt.model_validate(
                transaction_record.get("approval_receipt")
            )
        except Exception as exc:
            raise MemError(
                "USER_APPROVAL_REQUIRED",
                "pre_commit",
                str(exc),
                canonical_changed=False,
                retry_safe=False,
            ) from exc
        reviewed_hash = str(
            transaction_record.get("fingerprint_context", {}).get(
                "reviewed_proposal_hash"
            )
            or transaction_record.get("proposal_hash")
        )
        verify_approval_receipt(
            receipt=receipt,
            operations=operations,
            protected=protected,
            profile=profile,
            reviewed_proposal_hash=reviewed_hash,
            transaction_proposal_hash=str(transaction_record.get("proposal_hash")),
            transaction_fingerprint=str(
                transaction_record.get("transaction_fingerprint")
            ),
        )
    else:
        ensure(
            transaction_record.get("approval_receipt") is None,
            "APPROVAL_RECEIPT_UNEXPECTED",
            "pre_commit",
            "Unprotected transaction includes an approval receipt",
            canonical_changed=False,
            retry_safe=False,
        )
    return {
        "transaction_id": transaction_record.get("id"),
        "operations": len(operations),
        # Keep the old result key for consumers that inspect hook diagnostics;
        # its value is now Profile-driven and domain-neutral.
        "fast_path": is_fast_path(profile, operations),
        "messages_only": is_fast_path(profile, operations),
        "delta_by_stream": actual_by_stream,
    }


def _check_append_only(
    profile: Profile,
    bytes_by_stream: dict[str, tuple[bytes, bytes]],
    transaction_bytes: tuple[bytes, bytes],
) -> None:
    """Byte-level append-only guard for every stream, checked before any
    envelope or receipt logic so historical tampering is always reported as
    APPEND_ONLY_VIOLATION."""
    for name, stream in profile.config.streams.items():
        relative = Path(stream.path)
        old, new = bytes_by_stream[name]
        _appended_json(old, new, relative)
    transaction_path = Path("transactions/transactions.jsonl")
    _appended_json(transaction_bytes[0], transaction_bytes[1], transaction_path)


def _check_workspace_identity(root: Path) -> None:
    """Reject any staged rewrite or deletion of committed workspace identity."""

    relative = "workspace.json"
    original = _root_bytes(root, relative)
    if not original:
        return
    ensure(
        _staged_bytes(root, relative) == original,
        "WORKSPACE_INTEGRITY_ERROR",
        "pre_commit",
        "workspace.json differs from committed canonical identity",
        path=relative,
        canonical_changed=False,
        retry_safe=False,
        recovery=[
            "Restore the complete canonical repository containing its original initial commit"
        ],
    )


def _base_records_from_bytes(
    profile: Profile,
    bytes_by_stream: dict[str, tuple[bytes, bytes]],
) -> dict[str, list[dict[str, object]]]:
    """Parse the base (HEAD) JSONL bytes with strict envelope validation.

    This is the cheap historical guard for structured incremental validation:
    only envelope shape is checked, matching the transaction-side hot path.
    Schema/revision correctness of history is owned by the append-only guard,
    `mem git verify`, `mem doctor`, and `mem profile validate`.
    """
    base_records: dict[str, list[dict[str, object]]] = {}
    for stream_name, stream in profile.config.streams.items():
        old, _new = bytes_by_stream[stream_name]
        records: list[dict[str, object]] = []
        for line_number, line in enumerate(old.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MemError(
                    "JSONL_INVALID",
                    "envelope_validation",
                    str(exc),
                    stream=stream_name,
                    path=f"{stream.path}:{line_number}",
                    canonical_changed=False,
                    retry_safe=False,
                ) from exc
            try:
                RecordEnvelope.model_validate(record)
            except Exception as exc:
                raise MemError(
                    "ENVELOPE_INVALID",
                    "envelope_validation",
                    str(exc),
                    stream=stream_name,
                    record_id=record.get("id"),
                    canonical_changed=False,
                    retry_safe=False,
                ) from exc
            records.append(record)
        base_records[stream_name] = records
    return base_records


def _materialize_artifact_roots(
    root: Path,
    staged_tree: str,
    profile: Profile,
    target: Path,
) -> None:
    """Materialize only Profile artifact roots from the staged tree.

    Custom Validators may read artifact files (e.g. source snapshots). Full
    JSONL streams are not materialized; they are validated from bytes plus the
    transaction-shaped delta instead.
    """
    if not profile.config.artifact_roots:
        return
    _extract_archive(root, staged_tree, target, list(profile.config.artifact_roots))


def validate_repository(repo_root: Path) -> dict[str, object]:
    root = repo_root.resolve()
    has_head = _has_head(root)
    profile_tree = "HEAD" if has_head else str(_git(root, "write-tree")).strip()
    with tempfile.TemporaryDirectory(prefix="mem-core-profile-") as temporary:
        profile = _load_profile_from_tree(
            root,
            profile_tree,
            Path(temporary) / "trusted",
        )
        if not has_head:
            return {
                "ok": True,
                "profile": f"{profile.name}@{profile.version}",
                "mode": "root_initialization",
                "records": 0,
                "increment": {"root_initialization": True},
            }
        return _validate_increment(root, profile)


def _validate_increment(root: Path, profile: Profile) -> dict[str, object]:
    changed = set(
        str(_git(root, "diff", "--cached", "--name-only", "HEAD")).splitlines()
    )
    bytes_by_stream = {
        name: (_old_bytes(root, stream.path), _staged_bytes(root, stream.path))
        for name, stream in profile.config.streams.items()
    }
    transaction_path = "transactions/transactions.jsonl"
    transaction_bytes = (
        _old_bytes(root, transaction_path),
        _staged_bytes(root, transaction_path),
    )
    _check_append_only(profile, bytes_by_stream, transaction_bytes)
    _check_workspace_identity(root)
    increment = _verify_increment(
        root, profile, changed, bytes_by_stream, transaction_bytes
    )
    if increment.get("fast_path"):
        delta_by_stream = increment["delta_by_stream"]
        count = validate_delta_records(
            profile,
            {name: records for name, records in delta_by_stream.items() if records},
        )
        validation: dict[str, object] = {
            "ok": True,
            "profile": f"{profile.name}@{profile.version}",
            "mode": "incremental",
            "records": count,
            "delta": {
                name: len(records)
                for name, records in delta_by_stream.items()
                if records
            },
        }
    else:
        staged_tree = str(_git(root, "write-tree")).strip()
        base_records = _base_records_from_bytes(profile, bytes_by_stream)
        delta_by_stream = increment["delta_by_stream"]
        with tempfile.TemporaryDirectory(prefix="mem-core-hook-") as temporary:
            staged_root = Path(temporary) / "staged"
            staged_root.mkdir()
            _materialize_artifact_roots(root, staged_tree, profile, staged_root)
            count = validate_structured_delta(
                profile=profile,
                base_records=base_records,
                delta=delta_by_stream,
                root=staged_root,
            )
        validation = {
            "ok": True,
            "profile": f"{profile.name}@{profile.version}",
            "mode": "incremental",
            "records": count,
            "delta": {
                name: len(records)
                for name, records in delta_by_stream.items()
                if records
            },
        }
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
