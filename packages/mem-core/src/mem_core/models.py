from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def latest_by_id(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {}
    for record in records:
        previous = current.get(record["id"])
        if previous is None or record["revision"] > previous["revision"]:
            current[record["id"]] = record
    return current


class RecordEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    revision: int = Field(ge=1)
    recorded_at: str
    schema_version: str = Field(min_length=1)
    payload: dict[str, Any]

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_is_timezone_aware(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("recorded_at must include a timezone")
        return value


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["append", "write_artifact"]
    stream: str | None = None
    record: dict[str, Any] | None = None
    path: str | None = None
    content: str | None = None

    @model_validator(mode="after")
    def operation_shape(self) -> "Operation":
        if self.op == "append":
            if not self.stream or self.record is None:
                raise ValueError("append requires stream and record")
            if self.path is not None or self.content is not None:
                raise ValueError("append cannot include artifact path/content")
        else:
            if not self.path or self.content is None:
                raise ValueError("write_artifact requires path and content")
            if self.stream is not None or self.record is not None:
                raise ValueError("write_artifact cannot include stream/record")
        return self

    def normalized(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class ApprovalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    checkpoint_id: str
    decision: Literal["yes"]
    # Hash of the protected diff shown to the user.
    proposal_hash: str
    # Hash of the complete operation set, including unprotected audit records.
    transaction_proposal_hash: str
    transaction_fingerprint: str
    protected_operations_hash: str
    decided_at: str
    decision_message_id: str | None = None


class TransactionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal["open", "validated", "committed", "aborted"] = "open"
    profile_name: str
    profile_version: str
    base_commit: str
    fingerprint_context: dict[str, Any]
    operations: list[Operation] = Field(default_factory=list)
    transaction_fingerprint: str
    proposal_hash: str
    approval_receipt: ApprovalReceipt | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    committed_at: str | None = None
    commit: str | None = None
    validation: dict[str, Any] | None = None


def transaction_fingerprint(
    *,
    base_commit: str,
    profile_name: str,
    profile_version: str,
    fingerprint_context: dict[str, Any],
    operations: list[Operation],
) -> str:
    return sha256_json(
        {
            "base_commit": base_commit,
            "profile": f"{profile_name}@{profile_version}",
            "context": fingerprint_context,
            "operations": [op.normalized() for op in operations],
        }
    )


def proposal_hash(operations: list[Operation]) -> str:
    return sha256_json([op.normalized() for op in operations])


def protected_operations_hash(operations: list[Operation], protected: set[str]) -> str:
    return sha256_json(
        [
            op.normalized()
            for op in operations
            if op.op == "append" and op.stream in protected
        ]
    )
