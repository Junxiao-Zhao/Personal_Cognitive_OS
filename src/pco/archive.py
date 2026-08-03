from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from mem_core.models import Operation
from mem_core.transaction import TransactionManager

from .workspace import Workspace


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConversationArchive:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _record(self, message: dict[str, Any]) -> dict[str, Any]:
        thread = self.workspace.thread()
        binding = self.workspace.binding()
        role = message["role"]
        reasoning = message.get("reasoning") if self.workspace.config.archive_reasoning and role == "assistant" else None
        return {
            "id": message.get("id") or f"msg_{uuid.uuid4().hex}",
            "revision": 1,
            "recorded_at": message.get("recorded_at") or _now(),
            "schema_version": "conversation-message/v1",
            "payload": {
                "thread_id": thread.thread_id,
                "epoch_id": thread.active_epoch_id,
                "harness": binding.harness,
                "native_session_id": binding.native_session_id or message.get("native_session_id") or "unbound",
                "native_message_id": message["native_message_id"],
                "role": role,
                "kind": message.get("kind", "conversation"),
                "content": message.get("content", ""),
                "reasoning": reasoning,
                "refs": list(message.get("refs", [])),
                "created_at": message.get("created_at") or _now(),
            },
        }

    def archive(self, messages: Iterable[dict[str, Any]]) -> dict[str, Any]:
        allowed = [message for message in messages if message.get("role") in {"user", "assistant"} and message.get("kind", "conversation") in {"conversation", "checkpoint_decision"}]
        existing_keys = {
            (
                record["payload"]["harness"],
                record["payload"]["native_session_id"],
                record["payload"]["native_message_id"],
            )
            for record in self.workspace.repository.iter_records("messages")
        }
        records: list[dict[str, Any]] = []
        for message in allowed:
            record = self._record(message)
            key = (
                record["payload"]["harness"],
                record["payload"]["native_session_id"],
                record["payload"]["native_message_id"],
            )
            if key in existing_keys:
                continue
            existing_keys.add(key)
            records.append(record)
        if not records:
            return {"ok": True, "archived": 0, "commit": None}
        manager = TransactionManager(self.workspace.repository, self.workspace.config.state_root)
        state = manager.begin(
            transaction_id=f"txn_archive_{uuid.uuid4().hex}",
            fingerprint_context={"kind": "raw_archive", "native_message_ids": [record["payload"]["native_message_id"] for record in records]},
        )
        for record in records:
            manager.append(state.id, Operation(op="append", stream="messages", record=record))
        result = manager.commit(state.id)
        thread = self.workspace.thread()
        thread.last_archived_message_id = records[-1]["id"]
        thread.archive_cursor = records[-1]["payload"]["native_message_id"]
        self.workspace.save_thread(thread)
        return {"ok": True, "archived": len(records), "commit": result["commit"], "last_message_id": records[-1]["id"]}

    def archive_decision(
        self,
        *,
        checkpoint_id: str,
        proposal_hash: str,
        decision: Literal["yes", "no"] = "no",
        reason: str | None = None,
        native_message_id: str | None = None,
    ) -> dict[str, Any]:
        if decision == "no" and (reason is None or not reason.strip()):
            raise ValueError("A rejection reason or supplemental experience is required")
        content = reason.strip() if reason is not None and reason.strip() else "Yes — approve this exact Meta-memory proposal."
        message_id = f"msg_{uuid.uuid4().hex}"
        result = self.archive(
            [
                {
                    "id": message_id,
                    "native_message_id": native_message_id or f"decision_{uuid.uuid4().hex}",
                    "role": "user",
                    "kind": "checkpoint_decision",
                    "content": content,
                    "refs": [f"checkpoint:{checkpoint_id}", f"proposal:{proposal_hash}", f"decision:{decision}"],
                }
            ]
        )
        return {**result, "message_id": message_id}
