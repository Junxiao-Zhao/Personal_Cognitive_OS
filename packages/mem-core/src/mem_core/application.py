from __future__ import annotations

import errno
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from .errors import MemError, ensure
from .models import ApplicationCommitRequest, Operation


def application_transaction_id(transaction_digest: str) -> str:
    ensure(
        re.fullmatch(r"sha256:[0-9a-f]{64}", transaction_digest) is not None,
        "TRANSACTION_HASH_MISMATCH",
        "application_commit",
        "Invalid application transaction digest",
    )
    return "txn_" + transaction_digest.removeprefix("sha256:")


def application_identity_view(
    value: ApplicationCommitRequest | Mapping[str, Any],
) -> dict[str, Any]:
    """Return only the deterministic fields bound by an application digest."""

    if isinstance(value, ApplicationCommitRequest):
        data = value.model_dump(mode="json")
        data["transaction_id"] = application_transaction_id(value.transaction_digest)
    else:
        data = {
            "policy": value["application_policy"],
            "transaction_id": value["id"],
            "transaction_digest": value["transaction_fingerprint"],
            "base_commit": value["base_commit"],
            "operations": value["operations"],
            "receipt": value["receipt"],
        }
    receipt = deepcopy(data["receipt"])
    receipt.pop("committed_at", None)
    operations: list[dict[str, Any]] = []
    for raw in data["operations"]:
        operation = raw if isinstance(raw, Operation) else Operation.model_validate(raw)
        normalized = operation.model_dump(mode="json", exclude_none=True)
        if normalized.get("op") == "append" and isinstance(normalized.get("record"), dict):
            normalized["record"].pop("recorded_at", None)
        operations.append(normalized)
    return {
        "policy": data["policy"],
        "transaction_id": data["transaction_id"],
        "transaction_digest": data["transaction_digest"],
        "base_commit": data["base_commit"],
        "operations": operations,
        "receipt": receipt,
    }


def ensure_duplicate_matches_request(
    duplicate: Mapping[str, Any], request: ApplicationCommitRequest
) -> None:
    try:
        matches = application_identity_view(duplicate) == application_identity_view(request)
    except (KeyError, TypeError, ValueError) as exc:
        raise MemError(
            "WORKSPACE_INTEGRITY_ERROR",
            "application_commit",
            "Committed application transaction is malformed",
            retryable=False,
            recovery=["Restore the complete canonical repository from a trusted copy"],
        ) from exc
    ensure(
        matches,
        "WORKSPACE_INTEGRITY_ERROR",
        "application_commit",
        "Committed application transaction does not match the requested digest identity",
        retryable=False,
        recovery=["Restore the complete canonical repository from a trusted copy"],
    )


def rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno != errno.ENOTEMPTY:
            raise
