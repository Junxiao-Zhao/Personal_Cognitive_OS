from __future__ import annotations

import json
import uuid
from typing import Any, Iterable, Literal

from mem_core.models import Operation, utc_now
from mem_core.transaction import TransactionManager

from .workspace import Workspace


class ConversationArchive:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _record(self, message: dict[str, Any]) -> dict[str, Any]:
        thread = self.workspace.thread()
        binding = self.workspace.binding()
        role = message["role"]
        reasoning = message.get("reasoning") if self.workspace.config.archive_reasoning and role == "assistant" else None
        payload = {
            "id": message.get("id") or f"msg_{uuid.uuid4().hex}",
            "revision": 1,
            "recorded_at": message.get("recorded_at") or utc_now(),
            "schema_version": "conversation-message/v1",
            "payload": {
                "thread_id": thread.thread_id,
                "epoch_id": thread.active_epoch_id,
                "harness": binding.harness,
                "native_session_id": message.get("native_session_id") or binding.native_session_id or "unbound",
                "native_message_id": message["native_message_id"],
                "role": role,
                "kind": message.get("kind", "conversation"),
                "content": message.get("content", ""),
                "reasoning": reasoning,
                "refs": list(message.get("refs", [])),
                "created_at": message.get("created_at") or utc_now(),
            },
        }
        if "decision_provenance" in message:
            payload["payload"]["decision_provenance"] = message["decision_provenance"]
        return payload

    def archive(self, messages: Iterable[dict[str, Any]]) -> dict[str, Any]:
        allowed = [message for message in messages if message.get("role") in {"user", "assistant"} and message.get("kind", "conversation") in {"conversation", "checkpoint_decision"}]
        # The adapter returns messages strictly after the persisted cursor. If
        # the process crashed between the canonical commit and cursor update,
        # the complete committed batch is therefore at the canonical tail. The
        # tail window must cover the whole adapter batch: a fixed-size window
        # silently misses the beginning of batches larger than 64 messages.
        tail_keys = self._tail_native_keys(limit=max(64, len(allowed)))
        records: list[dict[str, Any]] = []
        for message in allowed:
            record = self._record(message)
            key = (
                record["payload"]["harness"],
                record["payload"]["native_session_id"],
                record["payload"]["native_message_id"],
            )
            if key in tail_keys:
                continue
            tail_keys.add(key)
            records.append(record)
        if not records:
            # A previous attempt may have committed the canonical records and
            # crashed before updating replaceable runtime state.  Recover the
            # cursor from the newest already-canonical input message.
            canonical_by_native_id = {
                record["payload"]["native_message_id"]: record
                for record in self.workspace.repository.iter_records("messages")
                if record["payload"]["harness"] == self.workspace.binding().harness
                and record["payload"]["native_session_id"]
                == (self.workspace.binding().native_session_id or "unbound")
            }
            archived_allowed = [message for message in allowed if message.get("native_message_id") in canonical_by_native_id]
            if archived_allowed:
                recovered = canonical_by_native_id[archived_allowed[-1]["native_message_id"]]
                thread = self.workspace.thread()
                thread.last_archived_message_id = recovered["id"]
                thread.archive_cursor = recovered["payload"]["native_message_id"]
                self.workspace.save_thread(thread)
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

    def _tail_native_keys(self, *, limit: int = 64) -> set[tuple[str, str, str]]:
        path = self.workspace.repository.profile.stream_path(self.workspace.config.memory_root, "messages")
        if not path.is_file():
            return set()
        keys: set[tuple[str, str, str]] = set()
        # Read backwards so the normal path remains bounded by the requested
        # tail size even when the canonical message stream is large.
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            chunks: list[bytes] = []
            newline_count = 0
            chunk_size = 64 * 1024
            # Read one extra delimiter when possible. The first line in the
            # assembled buffer may begin in the middle of a JSONL record; its
            # terminating newline must not consume one of the requested full
            # tail records.
            while position > 0 and newline_count <= limit:
                size = min(chunk_size, position)
                position -= size
                handle.seek(position)
                chunk = handle.read(size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
            data = b"".join(reversed(chunks))
            lines = data.splitlines()
            if position > 0 and lines:
                lines = lines[1:]
            lines = lines[-limit:]
        for line in lines[-limit:]:
            if line.strip():
                record = json.loads(line.decode("utf-8"))
                payload = record["payload"]
                keys.add((payload["harness"], payload["native_session_id"], payload["native_message_id"]))
        return keys

    def archive_decision(
        self,
        *,
        checkpoint_id: str,
        proposal_hash: str,
        decision: Literal["no"] = "no",
        reason: str,
        question_request_id: str,
        authorization_id: str,
        session_id: str | None = None,
        # Retained only so older callers fail closed through the new request
        # identity instead of accidentally reusing an assistant message ID.
        native_message_id: str | None = None,
    ) -> dict[str, Any]:
        if decision != "no":
            raise ValueError("archive_decision only accepts a native question rejection")
        if not reason.strip():
            raise ValueError("A rejection reason or supplemental experience is required")
        if native_message_id is not None:
            # The argument is deliberately ignored. The canonical identity
            # must be derived from the verified native question request.
            native_message_id = None
        if not question_request_id.strip():
            raise ValueError("A native question request ID is required for a rejection")
        if not authorization_id.strip():
            raise ValueError("A grant ID is required for a rejection")
        canonical_native_id = f"question:{question_request_id}"
        binding = self.workspace.binding()
        decision_session_id = session_id or binding.native_session_id or "unbound"
        provenance = {
            "checkpoint_id": checkpoint_id,
            "proposal_hash": proposal_hash,
            "question_request_id": question_request_id,
            "decision": "no",
            "authorization_id": authorization_id,
        }
        existing = next(
            (
                record
                for record in self.workspace.repository.iter_records("messages")
                if record["payload"].get("harness") == binding.harness
                and record["payload"].get("native_session_id") == decision_session_id
                and record["payload"].get("native_message_id") == canonical_native_id
            ),
            None,
        )
        if existing is not None:
            existing_provenance = existing["payload"].get("decision_provenance", {})
            immutable_provenance = ("checkpoint_id", "proposal_hash", "question_request_id", "decision")
            if any(existing_provenance.get(key) != provenance[key] for key in immutable_provenance) or existing["payload"].get("content") != reason:
                raise ValueError("A question request ID is already bound to a different decision")
            # A process may have crashed after archive() persisted the
            # decision and its synthetic cursor, but before the outer method
            # restored the real harness cursor.  A retry may legitimately use
            # a freshly issued one-shot grant; the canonical decision is
            # already authoritative, so recover the cursor from the messages
            # that precede this decision instead of treating the new grant as
            # a conflicting duplicate.
            self._restore_cursor_after_decision(existing, session_id=decision_session_id)
            return {"ok": True, "archived": 0, "commit": None, "message_id": existing["id"], "record": existing}
        content = reason
        message_id = f"msg_{uuid.uuid4().hex}"
        # The decision is canonical evidence, but its synthetic native ID is
        # not present in the harness transcript. Preserve the adapter cursor
        # while appending it so the next sync starts after a real message.
        thread_before = self.workspace.thread()
        result = self.archive(
            [
                {
                    "id": message_id,
                    "native_session_id": decision_session_id,
                    "native_message_id": canonical_native_id,
                    "role": "user",
                    "kind": "checkpoint_decision",
                    "content": content,
                    "refs": [f"checkpoint:{checkpoint_id}", f"proposal:{proposal_hash}", f"decision:{decision}"],
                    "decision_provenance": provenance,
                }
            ]
        )
        thread_after = self.workspace.thread()
        thread_after.archive_cursor = thread_before.archive_cursor
        thread_after.last_archived_message_id = thread_before.last_archived_message_id
        self.workspace.save_thread(thread_after)
        return {**result, "message_id": message_id}

    def _restore_cursor_after_decision(self, decision: dict[str, Any], *, session_id: str) -> None:
        thread = self.workspace.thread()
        if thread.archive_cursor is not None and not thread.archive_cursor.startswith("question:"):
            return
        records = list(self.workspace.repository.iter_records("messages"))
        harness = self.workspace.binding().harness
        decision_index = next(
            (index for index, record in enumerate(records) if record.get("id") == decision.get("id")),
            len(records),
        )
        recovered: dict[str, Any] | None = None
        for record in reversed(records[:decision_index]):
            payload = record.get("payload", {})
            if (
                payload.get("kind") == "conversation"
                and payload.get("harness") == harness
                and payload.get("native_session_id") == session_id
                and payload.get("native_message_id")
            ):
                recovered = record
                break
        thread.archive_cursor = recovered["payload"]["native_message_id"] if recovered else None
        thread.last_archived_message_id = recovered.get("id") if recovered else None
        self.workspace.save_thread(thread)
