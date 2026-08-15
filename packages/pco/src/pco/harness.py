from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx
from json_repair import repair_json

from mem_core.errors import MemError, ensure
from mem_core.models import Operation


WORKER_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["operations"],
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["op", "stream", "record"],
                        "properties": {
                            "op": {"const": "append"},
                            "stream": {"type": "string", "minLength": 1},
                            "record": {"type": "object"},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["op", "path", "content"],
                        "properties": {
                            "op": {"const": "write_artifact"},
                            "path": {"type": "string", "minLength": 1},
                            "content": {"type": "string"},
                        },
                    },
                ]
            },
        },
        "diagnostics": {"type": "array", "items": {"type": "object"}},
        "skill_versions": {"type": "object", "additionalProperties": {"type": "string"}},
    },
}


@dataclass(slots=True)
class WorkerHandle:
    id: str
    backend: str
    native_session_id: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "backend": self.backend, "native_session_id": self.native_session_id}


@dataclass(slots=True)
class WorkerResult:
    operations: list[Operation]
    search_receipts: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    skill_versions: dict[str, str] = field(default_factory=dict)
    runtime_info: dict[str, Any] = field(default_factory=dict)


class HarnessAdapter(Protocol):
    def attach_or_create(self) -> str: ...
    def archive_messages_since(self, cursor: str | None) -> list[dict[str, Any]]: ...
    def estimate_context_usage(self) -> float: ...
    def runtime_info(self) -> dict[str, Any]: ...
    def lock_input(self, checkpoint_id: str, state: str) -> None: ...
    def unlock_input(self) -> None: ...
    def spawn_worker(self, spec: dict[str, Any]) -> WorkerHandle: ...
    def resume_worker(self, handle: WorkerHandle, payload: dict[str, Any]) -> WorkerResult: ...
    def close_worker(self, handle: WorkerHandle) -> None: ...
    def compact(self) -> None: ...
    def publish_context(self, bundle: dict[str, Any]) -> None: ...
    def insert_receipt(self, receipt: dict[str, Any]) -> None: ...


class FakeHarnessAdapter:
    """Stateful conformance adapter used by acceptance tests and offline demos."""

    def __init__(
        self,
        state_root: Path,
        *,
        messages: list[dict[str, Any]] | None = None,
        worker: Callable[[dict[str, Any]], WorkerResult] | None = None,
        context_usage: float = 0.0,
    ) -> None:
        self.state_root = Path(state_root)
        self.messages = messages or []
        self.worker = worker or (lambda _payload: WorkerResult([]))
        self.context_usage = context_usage
        self.session_id = "ses_fake_main"
        self.published: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []
        self.compact_calls = 0
        self.worker_calls = 0
        self.resume_calls = 0
        self.closed_workers: list[str] = []
        self.actions: list[str] = []
        self.failures: dict[str, Exception] = {}

    def _maybe_fail(self, name: str) -> None:
        error = self.failures.pop(name, None)
        if error:
            raise error

    def attach_or_create(self) -> str:
        return self.session_id

    def archive_messages_since(self, cursor: str | None) -> list[dict[str, Any]]:
        if cursor is None:
            return list(self.messages)
        found = False
        result = []
        for message in self.messages:
            if found:
                result.append(message)
            if message["native_message_id"] == cursor:
                found = True
        return result if found else list(self.messages)

    def estimate_context_usage(self) -> float:
        return self.context_usage

    def runtime_info(self) -> dict[str, Any]:
        exposed = any(message.get("reasoning") for message in self.messages if message.get("role") == "assistant")
        return {
            "harness": "fake",
            "provider": "fixture",
            "model": "fixture",
            "reasoning_capability": "exposed_in_range" if exposed else "not_exposed_in_range",
        }

    def lock_input(self, checkpoint_id: str, state: str) -> None:
        self._maybe_fail("lock_input")
        self.actions.append(f"lock:{state}")
        path = self.state_root / "checkpoint-lock.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checkpoint_id": checkpoint_id, "state": state}, sort_keys=True), encoding="utf-8")

    def unlock_input(self) -> None:
        self.actions.append("unlock")
        path = self.state_root / "checkpoint-lock.json"
        path.unlink(missing_ok=True)

    def spawn_worker(self, spec: dict[str, Any]) -> WorkerHandle:
        self._maybe_fail("spawn_worker")
        self.worker_calls += 1
        self.actions.append("spawn_worker")
        return WorkerHandle(f"worker_{uuid.uuid4().hex}", "native_subagent", f"ses_fake_child_{self.worker_calls}")

    def resume_worker(self, handle: WorkerHandle, payload: dict[str, Any]) -> WorkerResult:
        self._maybe_fail("resume_worker")
        self.resume_calls += 1
        self.actions.append(f"resume_worker:{payload.get('kind')}")
        return self.worker(payload)

    def close_worker(self, handle: WorkerHandle) -> None:
        self._maybe_fail("close_worker")
        self.actions.append("close_worker")
        self.closed_workers.append(handle.id)

    def compact(self) -> None:
        self._maybe_fail("compact")
        self.compact_calls += 1
        self.actions.append("compact")

    def publish_context(self, bundle: dict[str, Any]) -> None:
        self._maybe_fail("publish_context")
        self.published.append(bundle)
        self.actions.append("publish_context")

    def insert_receipt(self, receipt: dict[str, Any]) -> None:
        self._maybe_fail("insert_receipt")
        self.receipts.append(receipt)
        self.actions.append("insert_receipt")


class OpenCodeAdapter:
    """OpenCode 1.17 HTTP adapter, isolated from the PCO state machine."""

    def __init__(
        self,
        *,
        base_url: str,
        directory: Path,
        state_root: Path,
        session_id: str | None = None,
        model_context_tokens: int = 128000,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.directory = directory.resolve()
        self.state_root = state_root.resolve()
        self.session_id = session_id
        self.model_context_tokens = model_context_tokens
        self.timeout_seconds = timeout
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout, trust_env=False)
        self._runtime_info: dict[str, Any] = {
            "harness": "opencode",
            "server_url": self.base_url,
            "provider": None,
            "model": None,
            "reasoning_capability": "not_exposed_in_range",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        params = dict(kwargs.pop("params", {}))
        if path.startswith("/session") or path.startswith("/tui"):
            params.setdefault("directory", str(self.directory))
        try:
            response = self.client.request(method, path, params=params, **kwargs)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MemError("HARNESS_REQUEST_FAILED", "harness", str(exc), retryable=True, recovery=["Check the OpenCode server and retry the checkpoint"]) from exc
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def attach_or_create(self) -> str:
        try:
            health = self._request("GET", "/global/health")
            self._runtime_info["harness_version"] = health.get("version")
        except MemError:
            self._runtime_info["harness_version"] = "unknown"
        if self.session_id:
            self._request("GET", f"/session/{self.session_id}")
            return self.session_id
        session = self._request("POST", "/session", json={"title": "PCO 主会话"})
        self.session_id = session["id"]
        return self.session_id

    @staticmethod
    def _visible_message(item: dict[str, Any]) -> dict[str, Any] | None:
        info = item.get("info", {})
        role = info.get("role")
        if role not in {"user", "assistant"}:
            return None
        # OpenCode's native compaction summary is synthetic context, not a
        # conversation turn.  The pre-compaction turns are already preserved
        # in the append-only raw archive.
        if info.get("summary") is True:
            return None
        text: list[str] = []
        reasoning: list[str] = []
        refs: list[str] = []
        for part in item.get("parts", []):
            kind = part.get("type")
            if part.get("metadata", {}).get("pco_control") is True:
                continue
            if kind == "text" and not part.get("synthetic", False):
                text.append(part.get("text", ""))
            elif kind == "reasoning":
                reasoning.append(part.get("text", ""))
            elif kind == "file" and part.get("url"):
                refs.append(part["url"])
        content = "\n".join(text)
        if not content and not refs:
            return None
        created = info.get("time", {}).get("created") or info.get("createdAt")
        if isinstance(created, (int, float)):
            created = datetime.fromtimestamp(created / 1000, timezone.utc).isoformat()
        return {
            "native_message_id": info["id"],
            "role": role,
            "kind": "conversation",
            "content": content,
            "reasoning": "\n".join(reasoning) or None,
            "refs": refs,
            "created_at": created,
        }

    def archive_messages_since(self, cursor: str | None) -> list[dict[str, Any]]:
        ensure(self.session_id is not None, "HARNESS_NOT_ATTACHED", "harness", "OpenCode session is not attached")
        page_size = 1000
        items: list[dict[str, Any]] = []
        before: str | None = None
        seen_boundaries: set[str] = set()
        cursor_found = cursor is None
        while True:
            params: dict[str, Any] = {"limit": page_size}
            if before is not None:
                params["before"] = before
            page = self._request("GET", f"/session/{self.session_id}/message", params=params)
            ensure(isinstance(page, list), "HARNESS_RESPONSE_INVALID", "harness", "OpenCode messages response is not a list")
            if not page:
                break
            items = page + items
            if cursor is not None and any(item.get("info", {}).get("id") == cursor for item in page):
                cursor_found = True
                break
            if len(page) < page_size:
                break
            boundary = page[0].get("info", {}).get("id")
            ensure(boundary, "HARNESS_RESPONSE_INVALID", "harness", "OpenCode message page has no boundary ID")
            ensure(boundary not in seen_boundaries, "HARNESS_PAGINATION_STALLED", "harness", "OpenCode message pagination did not advance")
            seen_boundaries.add(boundary)
            before = boundary
        ensure(cursor_found, "ARCHIVE_CURSOR_NOT_FOUND", "harness", "The archive cursor is no longer present in the complete OpenCode history", value=cursor)
        for item in reversed(items):
            info = item.get("info", {})
            if info.get("role") != "assistant":
                continue
            self._runtime_info["provider"] = info.get("providerID")
            self._runtime_info["model"] = info.get("modelID")
            self._runtime_info["reasoning_capability"] = (
                "exposed_in_range"
                if any(part.get("type") == "reasoning" and part.get("text") for part in item.get("parts", []))
                else "not_exposed_in_range"
            )
            break
        visible = [message for item in items if (message := self._visible_message(item)) is not None]
        if cursor is None:
            return visible
        for index, message in enumerate(visible):
            if message["native_message_id"] == cursor:
                return visible[index + 1 :]
        return visible

    def estimate_context_usage(self) -> float:
        ensure(self.session_id is not None, "HARNESS_NOT_ATTACHED", "harness", "OpenCode session is not attached")
        items = self._request("GET", f"/api/session/{self.session_id}/context")["data"]
        tokens = 0
        for item in items:
            info = item.get("info", item)
            usage = info.get("tokens", {})
            tokens += sum(value for value in usage.values() if isinstance(value, (int, float)))
            for part in item.get("parts", []):
                if part.get("type") in {"text", "reasoning"}:
                    tokens += len(part.get("text", "")) // 4
        return min(1.0, tokens / self.model_context_tokens)

    def runtime_info(self) -> dict[str, Any]:
        return dict(self._runtime_info)

    def lock_input(self, checkpoint_id: str, state: str) -> None:
        path = self.state_root / "checkpoint-lock.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checkpoint_id": checkpoint_id, "state": state}, sort_keys=True), encoding="utf-8")

    def unlock_input(self) -> None:
        (self.state_root / "checkpoint-lock.json").unlink(missing_ok=True)

    def spawn_worker(self, spec: dict[str, Any]) -> WorkerHandle:
        ensure(self.session_id is not None, "HARNESS_NOT_ATTACHED", "harness", "OpenCode session is not attached")
        session = self._request(
            "POST",
            "/session",
            json={"parentID": self.session_id, "title": f"PCO consolidate {spec['checkpoint_id']}", "agent": "pco-consolidator", "metadata": {"pco_worker_id": spec["worker_id"], "checkpoint_id": spec["checkpoint_id"]}},
        )
        return WorkerHandle(spec["worker_id"], "native_subagent", session["id"])

    def resume_worker(self, handle: WorkerHandle, payload: dict[str, Any]) -> WorkerResult:
        model = None
        if self._runtime_info.get("provider") and self._runtime_info.get("model"):
            model = {
                "providerID": self._runtime_info["provider"],
                "modelID": self._runtime_info["model"],
            }
        body: dict[str, Any] = {
            "agent": "pco-consolidator",
            "format": {"type": "json_schema", "schema": WORKER_RESULT_SCHEMA, "retryCount": 1},
            "parts": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        }
        if model is not None:
            body["model"] = model
        output_mode = "native_json_schema"
        try:
            response = self._request(
                "POST",
                f"/session/{handle.native_session_id}/message",
                json=body,
                timeout=min(30.0, self.timeout_seconds),
            )
            proposal, _repaired = self._worker_proposal(response, structured=True)
        except MemError as structured_error:
            # Not every OpenCode provider implements native JSON Schema output.
            # Cancel any in-flight schema generation and retry the same isolated
            # child once as JSON-only text; Pydantic/Profile validation remains
            # the authoritative result contract.
            try:
                self._request("POST", f"/session/{handle.native_session_id}/abort")
            except MemError:
                pass
            fallback_body: dict[str, Any] = {
                "agent": "pco-consolidator",
                "parts": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "kind": "validated_json_text_fallback",
                                "instruction": "Return exactly one JSON object matching contract. No Markdown fence or commentary.",
                                "contract": WORKER_RESULT_SCHEMA,
                                "payload": payload,
                                "native_schema_error": structured_error.detail.code,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            }
            if model is not None:
                fallback_body["model"] = model
            response = self._request(
                "POST",
                f"/session/{handle.native_session_id}/message",
                json=fallback_body,
            )
            try:
                proposal, repaired = self._worker_proposal(response, structured=False)
                output_mode = "validated_json_text_repair" if repaired else "validated_json_text_fallback"
            except MemError as fallback_error:
                try:
                    self._request("POST", f"/session/{handle.native_session_id}/abort")
                except MemError:
                    pass
                correction_body: dict[str, Any] = {
                    "agent": "pco-consolidator",
                    "parts": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "kind": "json_syntax_correction",
                                    "instruction": "Rewrite your immediately previous answer as one strictly valid JSON object matching the same contract. Preserve its semantic content. No Markdown fence or commentary.",
                                    "parse_error": fallback_error.detail.message,
                                },
                                ensure_ascii=False,
                            ),
                        }
                    ],
                }
                if model is not None:
                    correction_body["model"] = model
                response = self._request(
                    "POST",
                    f"/session/{handle.native_session_id}/message",
                    json=correction_body,
                )
                proposal, repaired = self._worker_proposal(response, structured=False)
                output_mode = "validated_json_text_correction_repair" if repaired else "validated_json_text_correction"
        try:
            operations = [Operation.model_validate(item) for item in proposal["operations"]]
        except Exception as exc:
            raise MemError("WORKER_RESULT_INVALID", "worker", str(exc), retryable=True, recovery=["Retry with the same frozen checkpoint input"]) from exc
        info = response.get("info", {})
        runtime_info = {
            "worker_provider": info.get("providerID"),
            "worker_model": info.get("modelID"),
            "worker_output_mode": output_mode,
            "worker_reasoning_capability": (
                "exposed"
                if any(part.get("type") == "reasoning" and part.get("text") for part in response.get("parts", []))
                else "not_exposed"
            ),
        }
        search_receipts: list[dict[str, Any]] = []
        for part in response.get("parts", []):
            state = part.get("state", {})
            tool_name = part.get("tool")
            if part.get("type") != "tool" or tool_name not in {"websearch", "webfetch"} or state.get("status") != "completed":
                continue
            call_id = str(part.get("callID") or part.get("id") or "")
            tool_input = state.get("input", {})
            tool_output = state.get("output", "")
            digest = hashlib.sha256(
                json.dumps(
                    {"call_id": call_id, "tool": tool_name, "input": tool_input, "output": tool_output},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()[:24]
            search_receipts.append(
                {
                    "id": f"search_{digest}",
                    "revision": 1,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "schema_version": "pco/search-receipt/v1",
                    "payload": {
                        "worker_session_id": handle.native_session_id,
                        "call_id": call_id,
                        "tool": tool_name,
                        "input": tool_input,
                        "output_excerpt": str(tool_output)[:4000],
                        "status": "completed",
                    },
                }
            )
        return WorkerResult(
            operations=operations,
            search_receipts=search_receipts,
            diagnostics=proposal.get("diagnostics", []),
            skill_versions=proposal.get("skill_versions", {}),
            runtime_info=runtime_info,
        )

    @staticmethod
    def _worker_proposal(response: dict[str, Any], *, structured: bool) -> tuple[dict[str, Any], bool]:
        info = response.get("info", {})
        if info.get("error"):
            error = info["error"]
            message = error.get("data", {}).get("message") if isinstance(error, dict) else str(error)
            raise MemError(
                "WORKER_MODEL_FAILED",
                "worker",
                message or "OpenCode worker model returned an error",
                retryable=True,
                recovery=["Retry with a model that supports the worker result contract"],
            )
        proposal = info.get("structured") if structured else None
        repaired = False
        if proposal is None:
            text = "\n".join(
                part.get("text", "")
                for part in response.get("parts", [])
                if part.get("type") == "text"
            ).strip()
            if text.startswith("```"):
                lines = text.splitlines()
                text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            start = text.find("{")
            if start < 0:
                raise MemError(
                    "WORKER_RESULT_INVALID",
                    "worker",
                    "OpenCode worker returned neither structured output nor a JSON object",
                    retryable=True,
                )
            try:
                proposal, _end = json.JSONDecoder().raw_decode(text[start:])
            except json.JSONDecodeError as exc:
                try:
                    proposal, _repairs = repair_json(
                        text[start:],
                        return_objects=True,
                        logging=True,
                        ensure_ascii=False,
                    )
                    repaired = True
                except Exception as repair_exc:
                    raise MemError("WORKER_RESULT_INVALID", "worker", str(exc), retryable=True) from repair_exc
        if not isinstance(proposal, dict):
            raise MemError("WORKER_RESULT_INVALID", "worker", "OpenCode worker output must be an object", retryable=True)
        return proposal, repaired

    def close_worker(self, handle: WorkerHandle) -> None:
        try:
            self._request("POST", f"/session/{handle.native_session_id}/abort")
        except MemError:
            # An idle/finished child may no longer be abortable; deletion remains
            # the authoritative reclamation operation.
            pass
        self._request("DELETE", f"/session/{handle.native_session_id}")

    def compact(self) -> None:
        ensure(self.session_id is not None, "HARNESS_NOT_ATTACHED", "harness", "OpenCode session is not attached")
        items = self._request("GET", f"/session/{self.session_id}/message", params={"limit": 10000})
        latest_conversation = -1
        latest_summary = -1
        provider_id: str | None = None
        model_id: str | None = None
        for index, item in enumerate(items):
            info = item.get("info", {})
            if info.get("role") == "assistant" and info.get("providerID") and info.get("modelID"):
                provider_id = info["providerID"]
                model_id = info["modelID"]
            if info.get("summary") is True:
                latest_summary = index
                continue
            if self._visible_message(item) is not None:
                latest_conversation = index

        # A retry may follow a lost/failed HTTP response after OpenCode already
        # finished compaction.  Treat that case as success instead of creating
        # a second summary.
        if latest_summary > latest_conversation:
            return

        ensure(provider_id is not None and model_id is not None, "HARNESS_MODEL_UNKNOWN", "harness", "Cannot compact without an OpenCode provider/model")
        result = self._request(
            "POST",
            f"/session/{self.session_id}/summarize",
            json={"providerID": provider_id, "modelID": model_id, "auto": False},
        )
        ensure(result is True, "HARNESS_COMPACTION_FAILED", "harness", "OpenCode did not confirm native compaction")

    def publish_context(self, bundle: dict[str, Any]) -> None:
        target = self.state_root / "context" / "bundle.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def insert_receipt(self, receipt: dict[str, Any]) -> None:
        self._request(
            "POST",
            "/tui/show-toast",
            json={"title": "PCO checkpoint", "message": receipt["summary"], "variant": "success" if receipt.get("ok") else "error", "duration": 10000},
        )
