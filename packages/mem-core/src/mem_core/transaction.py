from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .application import (
    application_transaction_id,
    ensure_duplicate_matches_request,
)
from .approval import verify_approval_receipt
from .delta import (
    is_fast_path,
    latest_base_by_id,
    show_bytes,
    validate_delta_records,
    validate_structured_delta,
)
from .errors import MemError, ensure
from .models import (
    ApplicationCommitRequest,
    ApprovalReceipt,
    Operation,
    TransactionState,
    proposal_hash,
    protected_operations_hash,
    transaction_fingerprint,
    utc_now,
)
from .repository import MemoryRepository


class TransactionManager:
    """Persistent transaction coordinator for a single canonical memory repo."""

    def __init__(self, repository: MemoryRepository, state_root: Path) -> None:
        self.repository = repository
        # Keep the caller-visible lexical path so later no-follow ownership
        # checks cannot be bypassed by resolving an attacker-provided symlink.
        self.state_root = state_root.absolute()
        self.transactions_root = self.state_root / "transactions"

    def _dir(self, transaction_id: str) -> Path:
        ensure(
            transaction_id and "/" not in transaction_id and ".." not in transaction_id,
            "TRANSACTION_ID_INVALID",
            "transaction",
            "Unsafe transaction ID",
            value=transaction_id,
            canonical_changed=False,
            retry_safe=False,
        )
        return self.transactions_root / transaction_id

    def _state_path(self, transaction_id: str) -> Path:
        return self._dir(transaction_id) / "state.json"

    def _worktree_path(self, transaction_id: str) -> Path:
        return self._dir(transaction_id) / "worktree"

    def _save(self, state: TransactionState) -> None:
        state.updated_at = utc_now()
        target = self._state_path(state.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, target)

    def load(self, transaction_id: str) -> TransactionState:
        path = self._state_path(transaction_id)
        ensure(
            path.is_file(),
            "TRANSACTION_NOT_FOUND",
            "transaction",
            f"Unknown transaction: {transaction_id}",
            canonical_changed=False,
            retry_safe=False,
        )
        try:
            return TransactionState.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise MemError(
                "TRANSACTION_STATE_INVALID",
                "transaction",
                str(exc),
                path=str(path),
                canonical_changed=False,
                retry_safe=False,
            ) from exc

    def begin(
        self,
        *,
        fingerprint_context: dict[str, Any],
        transaction_id: str | None = None,
    ) -> TransactionState:
        ensure(
            self.repository.profile.config.application_commit_policy == "public",
            "DIRECT_PCO_STREAM_WRITE_FORBIDDEN",
            "write_policy",
            "PCO business streams can only be written through pco proposal commit",
            canonical_changed=False,
            retry_safe=False,
        )
        return self._begin_public(
            fingerprint_context=fingerprint_context,
            transaction_id=transaction_id,
        )

    def _begin_public(
        self,
        *,
        fingerprint_context: dict[str, Any],
        transaction_id: str | None = None,
    ) -> TransactionState:
        self.repository.assert_clean()
        transaction_id = transaction_id or f"txn_{uuid.uuid4().hex}"
        ensure(
            not self._state_path(transaction_id).exists(),
            "TRANSACTION_EXISTS",
            "transaction",
            f"Transaction already exists: {transaction_id}",
            canonical_changed=False,
            retry_safe=False,
        )
        operations: list[Operation] = []
        base_commit = self.repository.head()
        state = TransactionState(
            id=transaction_id,
            profile_name=self.repository.profile.name,
            profile_version=self.repository.profile.version,
            base_commit=base_commit,
            fingerprint_context=fingerprint_context,
            operations=operations,
            transaction_fingerprint=transaction_fingerprint(
                base_commit=base_commit,
                profile_name=self.repository.profile.name,
                profile_version=self.repository.profile.version,
                fingerprint_context=fingerprint_context,
                operations=operations,
            ),
            proposal_hash=proposal_hash(operations),
        )
        self._save(state)
        return state

    def append(
        self, transaction_id: str, operation: Operation | dict[str, Any]
    ) -> TransactionState:
        state = self.load(transaction_id)
        ensure(
            state.status in {"open", "validated"},
            "TRANSACTION_CLOSED",
            "transaction",
            f"Transaction is {state.status}",
            canonical_changed=False,
            retry_safe=False,
        )
        op = (
            operation
            if isinstance(operation, Operation)
            else Operation.model_validate(operation)
        )
        if op.op == "append":
            ensure(
                op.record is not None,
                "RECORD_REQUIRED",
                "transaction",
                "append requires record",
                stream=op.stream,
                canonical_changed=False,
                retry_safe=False,
            )
            self.repository.profile.stream(op.stream or "")
        else:
            ensure(
                op.path is not None and op.content is not None,
                "ARTIFACT_REQUIRED",
                "transaction",
                "write_artifact requires path and content",
                canonical_changed=False,
                retry_safe=False,
            )
            self.repository.profile.artifact_path(self.repository.root, op.path)
        state.operations.append(op)
        state.status = "open"
        state.approval_receipt = None
        state.proposal_hash = proposal_hash(state.operations)
        state.transaction_fingerprint = transaction_fingerprint(
            base_commit=state.base_commit,
            profile_name=state.profile_name,
            profile_version=state.profile_version,
            fingerprint_context=state.fingerprint_context,
            operations=state.operations,
        )
        self._save(state)
        return state

    def protected_streams(self, state: TransactionState) -> set[str]:
        result: set[str] = set()
        for operation in state.operations:
            if operation.op != "append" or not operation.stream:
                continue
            policy = self.repository.profile.stream(operation.stream).write_policy
            if policy == "read_only":
                raise MemError(
                    "STREAM_READ_ONLY",
                    "write_policy",
                    f"Stream cannot be written: {operation.stream}",
                    stream=operation.stream,
                    canonical_changed=False,
                    retry_safe=False,
                )
            if policy == "user_approval":
                result.add(operation.stream)
        return result

    def attach_approval(
        self,
        transaction_id: str,
        *,
        checkpoint_id: str,
        proposal_hash_value: str,
        decision_message_id: str | None = None,
        receipt_id: str | None = None,
        authorization_id: str | None = None,
        authorization_source: str | None = None,
        authorization_provenance: dict[str, Any] | None = None,
    ) -> ApprovalReceipt:
        state = self.load(transaction_id)
        protected = self.protected_streams(state)
        ensure(
            protected,
            "APPROVAL_NOT_REQUIRED",
            "write_policy",
            "Transaction has no protected operations",
            canonical_changed=False,
            retry_safe=False,
        )
        reviewed_proposal_hash = str(
            state.fingerprint_context.get("reviewed_proposal_hash")
            or state.proposal_hash
        )
        ensure(
            proposal_hash_value == reviewed_proposal_hash,
            "PROPOSAL_HASH_MISMATCH",
            "write_policy",
            "Approval does not match the proposal under review",
            value=proposal_hash_value,
            canonical_changed=False,
            retry_safe=False,
        )
        receipt = ApprovalReceipt(
            id=receipt_id or f"approval_{uuid.uuid4().hex}",
            checkpoint_id=checkpoint_id,
            decision="yes",
            proposal_hash=reviewed_proposal_hash,
            transaction_proposal_hash=state.proposal_hash,
            transaction_fingerprint=state.transaction_fingerprint,
            protected_operations_hash=protected_operations_hash(
                state.operations, protected
            ),
            decided_at=utc_now(),
            decision_message_id=decision_message_id,
            authorization_id=authorization_id,
            authorization_source=authorization_source,
            authorization_provenance=authorization_provenance,
        )
        state.approval_receipt = receipt
        self._save(state)
        return receipt

    def _verify_approval(self, state: TransactionState, protected: set[str]) -> None:
        if not protected:
            return
        reviewed_proposal_hash = str(
            state.fingerprint_context.get("reviewed_proposal_hash")
            or state.proposal_hash
        )
        verify_approval_receipt(
            receipt=state.approval_receipt,
            operations=state.operations,
            protected=protected,
            profile=self.repository.profile,
            reviewed_proposal_hash=reviewed_proposal_hash,
            transaction_proposal_hash=state.proposal_hash,
            transaction_fingerprint=state.transaction_fingerprint,
        )

    def _prepare_worktree(self, state: TransactionState) -> Path:
        worktree = self._worktree_path(state.id)
        self.repository.remove_worktree(worktree)
        self.repository.add_worktree(worktree, state.base_commit)
        self._apply_operations(worktree, state.operations)
        return worktree

    def _apply_operations(self, worktree: Path, operations: list[Operation]) -> None:
        for operation in operations:
            if operation.op == "append":
                assert operation.stream is not None and operation.record is not None
                self.repository.append_record(
                    worktree, operation.stream, operation.record
                )
            else:
                assert operation.path is not None and operation.content is not None
                target = self.repository.profile.artifact_path(worktree, operation.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(operation.content, encoding="utf-8")

    def validate(
        self, transaction_id: str, *, require_approval: bool = False
    ) -> dict[str, Any]:
        state = self.load(transaction_id)
        ensure(
            state.status in {"open", "validated"},
            "TRANSACTION_CLOSED",
            "transaction_validation",
            f"Transaction is {state.status}",
            canonical_changed=False,
            retry_safe=False,
        )
        ensure(
            state.operations,
            "TRANSACTION_EMPTY",
            "transaction_validation",
            "Transaction has no operations",
            canonical_changed=False,
            retry_safe=False,
        )
        ensure(
            self.repository.head() == state.base_commit,
            "BASE_COMMIT_CHANGED",
            "transaction_validation",
            "Canonical memory advanced after transaction began",
            canonical_changed=False,
            retry_safe=False,
            recovery=["Begin a new transaction on the current canonical commit"],
        )
        protected = self.protected_streams(state)
        if require_approval:
            self._verify_approval(state, protected)
        profile = self.repository.profile
        delta: dict[str, list[dict[str, Any]]] = {}
        for op in state.operations:
            if op.op == "append" and op.stream:
                delta.setdefault(op.stream, []).append(op.record)
        if is_fast_path(profile, state.operations):
            current: dict[str, dict[str, dict[str, Any]]] = {}
            for stream_name in delta:
                stream_path = profile.stream(stream_name).path
                base_bytes = show_bytes(
                    self.repository.root, state.base_commit, stream_path
                )
                stream_current, baseline_reliable = latest_base_by_id(
                    base_bytes.decode("utf-8").splitlines()
                )
                # 基线不可靠（历史含损坏行）时跳过该 stream 的 revision
                # 连续性断言，避免误拒合法 delta（D6）。
                if baseline_reliable:
                    current[stream_name] = stream_current
            count = validate_delta_records(profile, delta, current)
            validation = {
                "ok": True,
                "profile": f"{profile.name}@{profile.version}",
                "mode": "incremental",
                "records": count,
                "delta": {name: len(records) for name, records in delta.items()},
            }
        else:
            worktree = self._prepare_worktree(state)
            # 严格 envelope 基线 + delta schema/revision + base+delta 合并 validator 视图。
            # 与 validate_all 同错码，但不再重复校验全部历史 schema/revision。
            base_records = {
                stream: list(self.repository.iter_records(stream))
                for stream in profile.config.streams
            }
            count = validate_structured_delta(
                profile=profile,
                base_records=base_records,
                delta=delta,
                root=worktree,
            )
            validation = {
                "ok": True,
                "profile": f"{profile.name}@{profile.version}",
                "mode": "incremental",
                "records": count,
                "delta": {name: len(records) for name, records in delta.items()},
            }
        state.status = "validated"
        state.validation = {
            **validation,
            "protected_streams": sorted(protected),
            "approval_required": bool(protected),
            "approval_attached": state.approval_receipt is not None,
            "proposal_hash": state.proposal_hash,
            "transaction_fingerprint": state.transaction_fingerprint,
        }
        self._save(state)
        return state.validation

    def _find_committed_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        head = self.repository.head()
        raw = show_bytes(
            self.repository.root,
            head,
            "transactions/transactions.jsonl",
        ).decode("utf-8")
        for line in raw.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("transaction_fingerprint") == fingerprint:
                record["commit"] = self.repository._git(
                    "log",
                    "--all",
                    "--format=%H",
                    "--fixed-strings",
                    "--grep",
                    f"memory transaction {record['id']}",
                    "-1",
                )
                return record
        return None

    def _application_attempt_dir(self, transaction_digest: str) -> Path:
        return (
            self.state_root
            / "application-worktrees"
            / application_transaction_id(transaction_digest)
        )

    def _application_worktree_path(self, transaction_digest: str) -> Path:
        return self._application_attempt_dir(transaction_digest) / "worktree"

    @staticmethod
    def _cleanup_identity(info: os.stat_result) -> tuple[int, int, int]:
        return (stat.S_IFMT(info.st_mode), info.st_dev, info.st_ino)

    @staticmethod
    def _cleanup_open_directory(
        parent_fd: int, name: str, display: Path
    ) -> tuple[int, tuple[int, int, int]]:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(observed.st_mode):
            raise OSError(
                errno.ENOTDIR,
                "owned cleanup component is not a directory",
                str(display),
            )
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        anchored = os.fstat(descriptor)
        identity = TransactionManager._cleanup_identity(anchored)
        if TransactionManager._cleanup_identity(observed) != identity:
            os.close(descriptor)
            raise OSError(
                errno.ESTALE,
                "owned cleanup component changed while opening",
                str(display),
            )
        return descriptor, identity

    @staticmethod
    def _cleanup_recheck(
        parent_fd: int,
        name: str,
        expected: tuple[int, int, int],
        display: Path,
    ) -> None:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if TransactionManager._cleanup_identity(current) != expected:
            raise OSError(
                errno.ESTALE, "owned cleanup component was replaced", str(display)
            )

    def _registered_worktree_paths(self) -> tuple[Path, ...]:
        raw = self.repository._git("worktree", "list", "--porcelain")
        paths: list[Path] = []
        for line in raw.splitlines():
            if line.startswith("worktree "):
                value = line.removeprefix("worktree ")
                if not value or value.startswith('"'):
                    raise ValueError("Git returned an ambiguous worktree path")
                paths.append(Path(value).absolute())
        if len(paths) != len(set(paths)):
            raise ValueError("Git returned duplicate worktree registrations")
        return tuple(paths)

    @staticmethod
    def _cleanup_rename_noreplace(
        parent_fd: int,
        source: str,
        target: str,
        expected: tuple[int, int, int],
    ) -> None:
        """Atomically detach one identity-bound child without replacing a name."""

        current = os.stat(source, dir_fd=parent_fd, follow_symlinks=False)
        if TransactionManager._cleanup_identity(current) != expected:
            raise OSError(errno.ESTALE, "application attempt changed before detach")
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        if (
            renameat2(
                parent_fd,
                os.fsencode(source),
                parent_fd,
                os.fsencode(target),
                1,  # RENAME_NOREPLACE
            )
            != 0
        ):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), target)

    @classmethod
    def _cleanup_owned_tree(cls, directory_fd: int, display: Path) -> None:
        """Remove only no-follow entries below an already-owned directory inode."""

        with os.scandir(directory_fd) as entries:
            children = sorted(entries, key=lambda entry: os.fsencode(entry.name))
        for child in children:
            child_display = display / child.name
            observed = child.stat(follow_symlinks=False)
            identity = cls._cleanup_identity(observed)
            if stat.S_ISDIR(observed.st_mode):
                child_fd, anchored = cls._cleanup_open_directory(
                    directory_fd, child.name, child_display
                )
                try:
                    cls._cleanup_owned_tree(child_fd, child_display)
                    cls._cleanup_recheck(
                        directory_fd, child.name, anchored, child_display
                    )
                    os.rmdir(child.name, dir_fd=directory_fd)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(observed.st_mode):
                file_fd = os.open(
                    child.name,
                    os.O_RDONLY
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_fd,
                )
                try:
                    anchored = cls._cleanup_identity(os.fstat(file_fd))
                    cls._cleanup_recheck(
                        directory_fd, child.name, anchored, child_display
                    )
                    if anchored != identity:
                        raise OSError(
                            errno.ESTALE, "cleanup file changed while opening"
                        )
                    os.unlink(child.name, dir_fd=directory_fd)
                finally:
                    os.close(file_fd)
            else:
                raise OSError(
                    errno.EPERM,
                    "application worktree contains an unsafe special entry",
                    str(child_display),
                )

    def _cleanup_application_attempt_impl(
        self, transaction_digest: str, *, review_id: str
    ) -> None:
        attempt = self._application_attempt_dir(transaction_digest)
        worktree = attempt / "worktree"
        registered = self._registered_worktree_paths()
        expected = worktree.absolute()
        expected_registrations = tuple(path for path in registered if path == expected)

        try:
            state_info = os.stat(self.state_root, follow_symlinks=False)
        except FileNotFoundError:
            if expected_registrations:
                self.repository._git("worktree", "prune")
                if expected in self._registered_worktree_paths():
                    raise OSError(
                        errno.EBUSY, "stale application worktree registration remains"
                    )
            return
        if not stat.S_ISDIR(state_info.st_mode):
            raise OSError(
                errno.ENOTDIR, "application state root is not a real directory"
            )
        state_fd = os.open(
            self.state_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        applications_fd: int | None = None
        attempt_fd: int | None = None
        worktree_fd: int | None = None
        try:
            state_identity = self._cleanup_identity(os.fstat(state_fd))
            if self._cleanup_identity(state_info) != state_identity:
                raise OSError(
                    errno.ESTALE, "application state root changed while opening"
                )
            try:
                applications_fd, applications_identity = self._cleanup_open_directory(
                    state_fd,
                    "application-worktrees",
                    self.state_root / "application-worktrees",
                )
            except FileNotFoundError:
                if expected_registrations:
                    self.repository._git("worktree", "prune")
                    if expected in self._registered_worktree_paths():
                        raise OSError(
                            errno.EBUSY,
                            "stale application worktree registration remains",
                        )
                return
            try:
                attempt_fd, attempt_identity = self._cleanup_open_directory(
                    applications_fd, attempt.name, attempt
                )
            except FileNotFoundError:
                if expected_registrations:
                    self.repository._git("worktree", "prune")
                    if expected in self._registered_worktree_paths():
                        raise OSError(
                            errno.EBUSY,
                            "stale application worktree registration remains",
                        )
                return

            with os.scandir(attempt_fd) as entries:
                names = sorted((entry.name for entry in entries), key=os.fsencode)
            if names not in ([], ["worktree"]):
                raise OSError(
                    errno.ENOTEMPTY,
                    "application attempt contains ambiguous extra entries",
                )
            if not names:
                if expected_registrations:
                    raise OSError(
                        errno.ESTALE, "registered application worktree is missing"
                    )
            else:
                if len(expected_registrations) != 1:
                    raise OSError(
                        errno.ESTALE,
                        "owned application worktree registration is missing or ambiguous",
                    )
                worktree_fd, worktree_identity = self._cleanup_open_directory(
                    attempt_fd, "worktree", worktree
                )
                if os.path.commonpath((str(attempt), str(expected))) != str(attempt):
                    raise OSError(
                        errno.EXDEV,
                        "application worktree escapes its attempt directory",
                    )
                registered_info = os.stat(expected, follow_symlinks=False)
                if self._cleanup_identity(registered_info) != worktree_identity:
                    raise OSError(
                        errno.ESTALE,
                        "Git registration does not name the anchored worktree",
                    )

                self._cleanup_recheck(
                    state_fd,
                    "application-worktrees",
                    applications_identity,
                    self.state_root / "application-worktrees",
                )
                self._cleanup_recheck(
                    applications_fd, attempt.name, attempt_identity, attempt
                )
                self._cleanup_recheck(
                    attempt_fd, "worktree", worktree_identity, worktree
                )
            quarantine_name = f".{attempt.name}.cleanup-{uuid.uuid4().hex}"
            self._cleanup_recheck(
                state_fd,
                "application-worktrees",
                applications_identity,
                self.state_root / "application-worktrees",
            )
            self._cleanup_recheck(
                applications_fd, attempt.name, attempt_identity, attempt
            )
            self._cleanup_rename_noreplace(
                applications_fd, attempt.name, quarantine_name, attempt_identity
            )
            try:
                os.stat(attempt.name, dir_fd=applications_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise OSError(
                    errno.ESTALE, "application attempt name remains after detach"
                )
            self._cleanup_recheck(
                applications_fd,
                quarantine_name,
                attempt_identity,
                self.state_root / "application-worktrees" / quarantine_name,
            )
            self._cleanup_owned_tree(
                attempt_fd,
                self.state_root / "application-worktrees" / quarantine_name,
            )
            self._cleanup_recheck(
                applications_fd,
                quarantine_name,
                attempt_identity,
                self.state_root / "application-worktrees" / quarantine_name,
            )
            os.rmdir(quarantine_name, dir_fd=applications_fd)
            self.repository._git("worktree", "prune")
            if expected in self._registered_worktree_paths():
                raise OSError(
                    errno.EBUSY, "Git worktree registration remains after cleanup"
                )
            try:
                with os.scandir(applications_fd) as entries:
                    applications_empty = not any(True for _entry in entries)
                if applications_empty:
                    self._cleanup_recheck(
                        state_fd,
                        "application-worktrees",
                        applications_identity,
                        self.state_root / "application-worktrees",
                    )
                    os.rmdir("application-worktrees", dir_fd=state_fd)
            except OSError as exc:
                if exc.errno != errno.ENOTEMPTY:
                    raise
        finally:
            for descriptor in (worktree_fd, attempt_fd, applications_fd, state_fd):
                if descriptor is not None:
                    os.close(descriptor)

    def cleanup_application_attempt(
        self, transaction_digest: str, *, review_id: str
    ) -> None:
        """Safely remove one proven, registered application worktree attempt."""

        try:
            self._cleanup_application_attempt_impl(
                transaction_digest, review_id=review_id
            )
        except Exception as exc:
            if (
                isinstance(exc, MemError)
                and exc.detail.code == "CANONICAL_WORKTREE_CLEANUP_FAILED"
            ):
                raise
            raise MemError(
                "CANONICAL_WORKTREE_CLEANUP_FAILED",
                "application_cleanup",
                "Committed application worktree ownership or cleanup could not be proven",
                path=str(self._application_attempt_dir(transaction_digest)),
                value=str(exc),
                canonical_changed=True,
                retry_safe=True,
                recovery=[f"pco proposal commit {review_id}"],
            ) from exc

    def _validate_application_operations(
        self,
        request: ApplicationCommitRequest,
        worktree: Path,
    ) -> dict[str, Any]:
        profile = self.repository.profile
        delta: dict[str, list[dict[str, Any]]] = {}
        for operation in request.operations:
            if operation.op == "append":
                assert operation.stream is not None and operation.record is not None
                stream = profile.stream(operation.stream)
                ensure(
                    stream.write_policy != "read_only",
                    "STREAM_READ_ONLY",
                    "write_policy",
                    f"Stream cannot be written: {operation.stream}",
                    stream=operation.stream,
                    canonical_changed=False,
                    retry_safe=False,
                )
                delta.setdefault(operation.stream, []).append(operation.record)
            else:
                assert operation.path is not None and operation.content is not None
                profile.artifact_path(worktree, operation.path)

        if is_fast_path(profile, request.operations):
            current: dict[str, dict[str, dict[str, Any]]] = {}
            for stream_name in delta:
                stream_path = profile.stream(stream_name).path
                base_bytes = show_bytes(
                    self.repository.root, request.base_commit, stream_path
                )
                stream_current, baseline_reliable = latest_base_by_id(
                    base_bytes.decode("utf-8").splitlines()
                )
                if baseline_reliable:
                    current[stream_name] = stream_current
            count = validate_delta_records(profile, delta, current)
        else:
            base_records = {
                stream: list(self.repository.iter_records(stream))
                for stream in profile.config.streams
            }
            count = validate_structured_delta(
                profile=profile,
                base_records=base_records,
                delta=delta,
                root=worktree,
            )
        return {
            "ok": True,
            "profile": f"{profile.name}@{profile.version}",
            "mode": "incremental",
            "records": count,
            "delta": {name: len(records) for name, records in delta.items()},
        }

    def _commit_application_worktree(
        self, request: ApplicationCommitRequest
    ) -> dict[str, Any]:
        transaction_id = application_transaction_id(request.transaction_digest)
        worktree = self._application_worktree_path(request.transaction_digest)
        self.repository.assert_clean()
        self.repository.remove_worktree(worktree)
        self.repository.add_worktree(worktree, request.base_commit)
        self._apply_operations(worktree, request.operations)
        validation = self._validate_application_operations(request, worktree)
        transaction_record = {
            "id": transaction_id,
            "application_policy": request.policy,
            "base_commit": request.base_commit,
            "operation_count": len(request.operations),
            "operations": [operation.normalized() for operation in request.operations],
            "transaction_fingerprint": request.transaction_digest,
            "receipt": request.receipt,
        }
        txn_log = worktree / "transactions" / "transactions.jsonl"
        with txn_log.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(transaction_record, ensure_ascii=False, sort_keys=True)
                + "\n"
            )
        self.repository._git("add", ".", cwd=worktree)
        # The generic hook verifies the public TransactionState receipt shape.
        # This internal path performs the same incremental validation above and
        # commits its distinct application receipt shape atomically.
        self.repository._git(
            "commit",
            "--no-verify",
            "-m",
            f"memory transaction {transaction_id}",
            cwd=worktree,
        )
        commit = self.repository._git("rev-parse", "HEAD", cwd=worktree)
        self.repository.fast_forward(commit)
        self.cleanup_application_attempt(
            request.transaction_digest,
            review_id=str(request.receipt["review_id"]),
        )
        return {
            "ok": True,
            "idempotent": False,
            "transaction_id": transaction_id,
            "commit": commit,
            "validation": validation,
        }

    def commit_application(
        self,
        request: ApplicationCommitRequest,
        *,
        lock_held: bool = False,
    ) -> dict[str, Any]:
        ensure(
            request.policy == self.repository.profile.config.application_commit_policy,
            "DIRECT_PCO_STREAM_WRITE_FORBIDDEN",
            "write_policy",
            "Application commit policy mismatch",
            canonical_changed=False,
            retry_safe=False,
        )
        context = nullcontext() if lock_held else self.repository.lock()
        with context:
            duplicate = self._find_committed_fingerprint(request.transaction_digest)
            if duplicate is not None:
                ensure_duplicate_matches_request(duplicate, request)
                self.cleanup_application_attempt(
                    request.transaction_digest,
                    review_id=str(request.receipt["review_id"]),
                )
                return {
                    "ok": True,
                    "idempotent": True,
                    "commit": duplicate["commit"],
                    "transaction": duplicate,
                }
            ensure(
                self.repository.head() == request.base_commit,
                "CANONICAL_HEAD_CHANGED",
                "transaction_validation",
                "Canonical HEAD changed before commit",
                canonical_changed=False,
                retry_safe=False,
                recovery=[
                    "Create a new proposal from pco proposal scaffold against the current HEAD"
                ],
            )
            return self._commit_application_worktree(request)

    def commit(self, transaction_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            validation = self.validate(transaction_id, require_approval=True)
            state = self.load(transaction_id)
            return {
                "ok": True,
                "dry_run": True,
                "transaction_id": transaction_id,
                "validation": validation,
                "would_commit": True,
            }
        state = self.load(transaction_id)
        if state.status == "committed":
            return {
                "ok": True,
                "idempotent": True,
                "transaction_id": state.id,
                "commit": state.commit,
            }
        duplicate = self._find_committed_fingerprint(state.transaction_fingerprint)
        if duplicate:
            state.status = "committed"
            state.committed_at = duplicate.get("committed_at") or utc_now()
            state.commit = duplicate["commit"]
            self._save(state)
            self.repository.remove_worktree(self._worktree_path(state.id))
            return {
                "ok": True,
                "idempotent": True,
                "transaction_id": state.id,
                "duplicate_of": duplicate["id"],
                "commit": duplicate["commit"],
            }
        validation = self.validate(transaction_id, require_approval=True)
        state = self.load(transaction_id)
        worktree = self._worktree_path(transaction_id)
        if not (worktree / ".git").exists():
            # messages-only 快路径不建 worktree（validate 无副作用）；结构化路径已复用现有 worktree
            worktree = self._prepare_worktree(state)
        transaction_record = {
            "id": state.id,
            "transaction_fingerprint": state.transaction_fingerprint,
            "proposal_hash": state.proposal_hash,
            "base_commit": state.base_commit,
            "profile": f"{state.profile_name}@{state.profile_version}",
            "fingerprint_context": state.fingerprint_context,
            "operation_count": len(state.operations),
            "operations": [operation.normalized() for operation in state.operations],
            "approval_receipt": (
                state.approval_receipt.model_dump(mode="json")
                if state.approval_receipt
                else None
            ),
            "committed_at": utc_now(),
        }
        txn_log = worktree / "transactions" / "transactions.jsonl"
        with txn_log.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(transaction_record, ensure_ascii=False, sort_keys=True)
                + "\n"
            )
        self.repository.install_pre_commit_hook()
        self.repository._git("add", ".", cwd=worktree)
        self.repository._git(
            "commit", "-m", f"memory transaction {state.id}", cwd=worktree
        )
        commit = self.repository._git("rev-parse", "HEAD", cwd=worktree)
        self.repository.fast_forward(commit)
        state.status = "committed"
        state.committed_at = utc_now()
        state.commit = commit
        self._save(state)
        self.repository.remove_worktree(worktree)
        return {
            "ok": True,
            "idempotent": False,
            "transaction_id": state.id,
            "commit": commit,
            "validation": validation,
        }

    def abort(self, transaction_id: str) -> dict[str, Any]:
        state = self.load(transaction_id)
        ensure(
            state.status != "committed",
            "TRANSACTION_ALREADY_COMMITTED",
            "transaction",
            "Committed transactions cannot be aborted",
            canonical_changed=False,
            retry_safe=False,
        )
        self.repository.remove_worktree(self._worktree_path(transaction_id))
        state.status = "aborted"
        self._save(state)
        return {"ok": True, "transaction_id": state.id, "status": state.status}

    def status(self, transaction_id: str) -> dict[str, Any]:
        state = self.load(transaction_id)
        return {"ok": True, "transaction": state.model_dump(mode="json")}
