from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .approval import verify_approval_receipt
from .delta import is_messages_only, latest_base_by_id, show_bytes, validate_delta_records
from .errors import MemError, ensure
from .models import (
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
        self.state_root = state_root.resolve()
        self.transactions_root = self.state_root / "transactions"
        self.transactions_root.mkdir(parents=True, exist_ok=True)

    def _dir(self, transaction_id: str) -> Path:
        ensure(
            transaction_id and "/" not in transaction_id and ".." not in transaction_id,
            "TRANSACTION_ID_INVALID",
            "transaction",
            "Unsafe transaction ID",
            value=transaction_id,
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
        ensure(path.is_file(), "TRANSACTION_NOT_FOUND", "transaction", f"Unknown transaction: {transaction_id}")
        try:
            return TransactionState.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise MemError("TRANSACTION_STATE_INVALID", "transaction", str(exc), path=str(path)) from exc

    def begin(
        self,
        *,
        fingerprint_context: dict[str, Any],
        transaction_id: str | None = None,
    ) -> TransactionState:
        self.repository.assert_clean()
        transaction_id = transaction_id or f"txn_{uuid.uuid4().hex}"
        ensure(not self._state_path(transaction_id).exists(), "TRANSACTION_EXISTS", "transaction", f"Transaction already exists: {transaction_id}")
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

    def append(self, transaction_id: str, operation: Operation | dict[str, Any]) -> TransactionState:
        state = self.load(transaction_id)
        ensure(state.status in {"open", "validated"}, "TRANSACTION_CLOSED", "transaction", f"Transaction is {state.status}")
        op = operation if isinstance(operation, Operation) else Operation.model_validate(operation)
        if op.op == "append":
            ensure(op.record is not None, "RECORD_REQUIRED", "transaction", "append requires record", stream=op.stream)
            self.repository.profile.stream(op.stream or "")
        else:
            ensure(op.path is not None and op.content is not None, "ARTIFACT_REQUIRED", "transaction", "write_artifact requires path and content")
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
    ) -> ApprovalReceipt:
        state = self.load(transaction_id)
        protected = self.protected_streams(state)
        ensure(protected, "APPROVAL_NOT_REQUIRED", "write_policy", "Transaction has no protected operations")
        reviewed_proposal_hash = str(state.fingerprint_context.get("reviewed_proposal_hash") or state.proposal_hash)
        ensure(
            proposal_hash_value == reviewed_proposal_hash,
            "PROPOSAL_HASH_MISMATCH",
            "write_policy",
            "Approval does not match the proposal under review",
            value=proposal_hash_value,
        )
        receipt = ApprovalReceipt(
            id=receipt_id or f"approval_{uuid.uuid4().hex}",
            checkpoint_id=checkpoint_id,
            decision="yes",
            proposal_hash=reviewed_proposal_hash,
            transaction_proposal_hash=state.proposal_hash,
            transaction_fingerprint=state.transaction_fingerprint,
            protected_operations_hash=protected_operations_hash(state.operations, protected),
            decided_at=utc_now(),
            decision_message_id=decision_message_id,
        )
        state.approval_receipt = receipt
        self._save(state)
        return receipt

    def _verify_approval(self, state: TransactionState, protected: set[str]) -> None:
        if not protected:
            return
        reviewed_proposal_hash = str(state.fingerprint_context.get("reviewed_proposal_hash") or state.proposal_hash)
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
        for operation in state.operations:
            if operation.op == "append":
                assert operation.stream is not None and operation.record is not None
                self.repository.append_record(worktree, operation.stream, operation.record)
            else:
                assert operation.path is not None and operation.content is not None
                target = self.repository.profile.artifact_path(worktree, operation.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(operation.content, encoding="utf-8")
        return worktree

    def validate(self, transaction_id: str, *, require_approval: bool = False) -> dict[str, Any]:
        state = self.load(transaction_id)
        ensure(state.status in {"open", "validated"}, "TRANSACTION_CLOSED", "transaction_validation", f"Transaction is {state.status}")
        ensure(state.operations, "TRANSACTION_EMPTY", "transaction_validation", "Transaction has no operations")
        ensure(
            self.repository.head() == state.base_commit,
            "BASE_COMMIT_CHANGED",
            "transaction_validation",
            "Canonical memory advanced after transaction began",
            retryable=True,
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
        if is_messages_only(state.operations):
            messages_path = profile.stream("messages").path
            base_bytes = show_bytes(self.repository.root, state.base_commit, messages_path)
            current, baseline_reliable = latest_base_by_id(base_bytes.decode("utf-8").splitlines())
            # 基线不可靠（历史含损坏行）时跳过 revision 连续性断言，避免误拒合法 delta（D6）
            count = validate_delta_records(profile, delta, {"messages": current} if baseline_reliable else None)
            validation = {"ok": True, "profile": f"{profile.name}@{profile.version}", "mode": "incremental",
                          "records": count, "delta": {name: len(records) for name, records in delta.items()}}
        else:
            worktree = self._prepare_worktree(state)
            validation = {**self.repository.validate_all(root=worktree), "mode": "full"}
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
        path = self.repository.root / "transactions" / "transactions.jsonl"
        if not path.exists():
            return None
        for line in path.read_text(encoding="utf-8").splitlines():
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
            return {"ok": True, "idempotent": True, "transaction_id": state.id, "commit": state.commit}
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
            "approval_receipt": state.approval_receipt.model_dump(mode="json") if state.approval_receipt else None,
            "committed_at": utc_now(),
        }
        txn_log = worktree / "transactions" / "transactions.jsonl"
        with txn_log.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(transaction_record, ensure_ascii=False, sort_keys=True) + "\n")
        self.repository.install_pre_commit_hook()
        self.repository._git("add", ".", cwd=worktree)
        self.repository._git("commit", "-m", f"memory transaction {state.id}", cwd=worktree)
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
        ensure(state.status != "committed", "TRANSACTION_ALREADY_COMMITTED", "transaction", "Committed transactions cannot be aborted")
        self.repository.remove_worktree(self._worktree_path(transaction_id))
        state.status = "aborted"
        self._save(state)
        return {"ok": True, "transaction_id": state.id, "status": state.status}

    def status(self, transaction_id: str) -> dict[str, Any]:
        state = self.load(transaction_id)
        return {"ok": True, "transaction": state.model_dump(mode="json")}
