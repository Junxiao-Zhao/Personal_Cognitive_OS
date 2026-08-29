from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from json_repair import repair_json

from mem_core.errors import MemError, ensure
from mem_core.models import Operation


_EXTERNAL_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)


def normalize_external_url(value: Any) -> str | None:
    """Return the comparison form used for external-reference provenance."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().rstrip(".,;:!?)]}")
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return None
        port = parsed.port
    except ValueError:
        return None
    netloc = hostname.lower()
    if parsed.username is not None or parsed.password is not None:
        credentials = parsed.username or ""
        if parsed.password is not None:
            credentials += f":{parsed.password}"
        netloc = f"{credentials}@{netloc}"
    if port is not None and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def extract_result_urls(tool: str, tool_input: Any, tool_output: Any, *, status: str, error: Any = None) -> list[str]:
    """Extract only wrapper-observed result URLs from a completed tool call.

    Search results come exclusively from the tool output.  Fetch results use
    only the requested target URL, and only after a successful completion.
    """
    if status != "completed" or error:
        return []
    if tool == "websearch":
        if isinstance(tool_output, str):
            output_text = tool_output
        else:
            output_text = json.dumps(tool_output, ensure_ascii=False, sort_keys=True, default=str)
        candidates = _EXTERNAL_URL_RE.findall(output_text)
    elif tool == "webfetch":
        candidates = [tool_input.get("url")] if isinstance(tool_input, dict) else []
    else:
        candidates = []
    normalized = {url for candidate in candidates if (url := normalize_external_url(candidate))}
    return sorted(normalized)


def receipt_result_urls(receipt: dict[str, Any]) -> list[str]:
    """Read or minimally backfill result_urls on a newly captured receipt.

    v1 receipts already in the repository are not re-versioned.  The fallback
    is only for an incoming wrapper capture whose output fields predate the
    result_urls extension; it records the derived list while retaining v1.
    """
    payload = receipt.get("payload")
    if not isinstance(payload, dict):
        return []
    if "result_urls" in payload:
        raw_urls = payload["result_urls"]
        if not isinstance(raw_urls, list):
            return []
        urls = [url for item in raw_urls if (url := normalize_external_url(item))]
        if len(urls) != len(raw_urls):
            return []
        payload["result_urls"] = sorted(set(urls))
        return payload["result_urls"]
    urls = extract_result_urls(
        str(payload.get("tool", "")),
        payload.get("input", {}),
        payload.get("output_excerpt", ""),
        status=str(payload.get("status", "")),
    )
    if urls:
        payload["result_urls"] = urls
    return urls


def receipt_payload_hash(receipt: dict[str, Any]) -> str:
    """Hash the immutable receipt payload, excluding delivery acknowledgements."""

    immutable = {
        key: value
        for key, value in receipt.items()
        if key not in {"host_receipt_generation", "receipt_delivery"}
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(immutable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


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


@dataclass(frozen=True, slots=True)
class NativeCompactBypass:
    """Private one-shot binding used for a PCO-owned native compact."""

    token: str
    checkpoint_id: str
    session_id: str
    attempt_id: str
    expires_at: int

    @classmethod
    def from_value(cls, value: "NativeCompactBypass | dict[str, Any]") -> "NativeCompactBypass":
        if isinstance(value, cls):
            return value
        checkpoint_id = value.get("checkpoint_id", value.get("checkpointID"))
        session_id = value.get("session_id", value.get("sessionID"))
        attempt_id = value.get("attempt_id", value.get("attemptID"))
        expires_at = value.get("expires_at", value.get("expiresAt"))
        ensure(
            isinstance(value.get("token"), str) and value["token"],
            "NATIVE_COMPACT_TOKEN_INVALID",
            "native_compact",
            "Missing native compact bypass token",
        )
        ensure(
            isinstance(checkpoint_id, str) and checkpoint_id,
            "NATIVE_COMPACT_TOKEN_INVALID",
            "native_compact",
            "Missing native compact checkpoint binding",
        )
        ensure(
            isinstance(session_id, str) and session_id,
            "NATIVE_COMPACT_TOKEN_INVALID",
            "native_compact",
            "Missing native compact session binding",
        )
        ensure(
            isinstance(attempt_id, str) and attempt_id,
            "NATIVE_COMPACT_TOKEN_INVALID",
            "native_compact",
            "Missing native compact attempt binding",
        )
        ensure(
            isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool),
            "NATIVE_COMPACT_TOKEN_INVALID",
            "native_compact",
            "Missing native compact token expiry",
        )
        return cls(
            token=value["token"],
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            attempt_id=attempt_id,
            expires_at=int(expires_at),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "checkpointID": self.checkpoint_id,
            "sessionID": self.session_id,
            "attemptID": self.attempt_id,
            "expiresAt": self.expires_at,
        }


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
    def compact(self, bypass: NativeCompactBypass | dict[str, Any] | None = None) -> None: ...
    def publish_context(self, bundle: dict[str, Any]) -> None: ...
    def publish_receipt(self, receipt: dict[str, Any], outbox: dict[str, Any]) -> dict[str, Any]: ...


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
        self.receipt_resources: dict[str, dict[str, Any]] = {}

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

    def compact(self, bypass: NativeCompactBypass | dict[str, Any] | None = None) -> None:
        self._maybe_fail("compact")
        self.compact_calls += 1
        self.actions.append("compact")

    def publish_context(self, bundle: dict[str, Any]) -> None:
        self._maybe_fail("publish_context")
        self.published.append(bundle)
        self.actions.append("publish_context")

    def publish_receipt(self, receipt: dict[str, Any], outbox: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail("publish_receipt")
        key = str(outbox["receipt_key"])
        payload_hash = str(outbox["payload_hash"])
        existing = self.receipt_resources.get(key)
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise MemError("RECEIPT_KEY_CONFLICT", "receipt", "Receipt key was reused with a different payload", retryable=False)
            self.actions.append("receipt_existing")
            return {**existing, "disposition": "existing"}
        resource = {
            "host_resource_id": f"fake_receipt_{uuid.uuid4().hex}",
            "key": key,
            "generation": int(outbox["generation"]),
            "payload_hash": payload_hash,
            "disposition": "created",
        }
        self.receipt_resources[key] = resource
        self.receipts.append(receipt)
        self.actions.append("insert_receipt")
        return resource

    def insert_receipt(self, receipt: dict[str, Any]) -> None:
        # Compatibility entry point for older direct adapter callers. The
        # checkpoint finalizer uses publish_receipt and its durable outbox.
        key = str(receipt.get("receipt_key") or f"legacy:{uuid.uuid4().hex}")
        outbox = {"receipt_key": key, "generation": int(receipt.get("receipt_generation", 0)), "payload_hash": receipt_payload_hash(receipt)}
        self.publish_receipt(receipt, outbox)


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
        native_compact_bypass: NativeCompactBypass | dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.directory = directory.resolve()
        self.state_root = state_root.resolve()
        self.session_id = session_id
        self.model_context_tokens = model_context_tokens
        self.timeout_seconds = timeout
        self.native_compact_bypass = native_compact_bypass
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout, trust_env=False)
        self._runtime_info: dict[str, Any] = {
            "harness": "opencode",
            "server_url": self.base_url,
            "provider": None,
            "model": None,
            "reasoning_capability": "not_exposed_in_range",
        }
        self._model_context_limits: dict[tuple[str | None, str | None], int] = {}
        self._model_metadata_loaded: set[tuple[str | None, str | None]] = set()
        self._runtime_context_limit_key: tuple[str | None, str | None] | None = None

    def _set_model_identity(self, provider: Any, model: Any) -> None:
        """Update the active model and discard a limit belonging to another model."""
        next_provider = provider if isinstance(provider, str) and provider else None
        next_model = model if isinstance(model, str) and model else None
        previous = (self._runtime_info.get("provider"), self._runtime_info.get("model"))
        current = (next_provider, next_model)
        self._runtime_info["provider"] = next_provider
        self._runtime_info["model"] = next_model
        if previous != current:
            self._runtime_info.pop("context_limit", None)
            self._runtime_context_limit_key = None

    def _active_model_key(self) -> tuple[str | None, str | None]:
        return (self._runtime_info.get("provider"), self._runtime_info.get("model"))

    def _clear_stale_runtime_context_limit(self) -> tuple[str | None, str | None]:
        key = self._active_model_key()
        if self._runtime_context_limit_key is not None and self._runtime_context_limit_key != key:
            self._runtime_info.pop("context_limit", None)
            self._runtime_context_limit_key = None
        return key

    @staticmethod
    def _context_limit_from(value: Any) -> int | None:
        if isinstance(value, dict):
            model_limit = value.get("limit")
            if isinstance(model_limit, dict):
                candidate = model_limit.get("context")
                if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and candidate > 0:
                    return int(candidate)
            for key in (
                "context_limit",
                "contextLimit",
                "context_length",
                "contextLength",
                "context_window",
                "contextWindow",
                "max_input_tokens",
                "maxInputTokens",
            ):
                candidate = value.get(key)
                if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and candidate > 0:
                    return int(candidate)
            for key in ("model", "metadata", "limits", "capabilities"):
                candidate = value.get(key)
                found = OpenCodeAdapter._context_limit_from(candidate)
                if found is not None:
                    return found
        return None

    def _remember_provider_catalog(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        providers = value.get("all") or value.get("providers")
        if not isinstance(providers, list):
            return
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            provider_id = provider.get("id")
            models = provider.get("models")
            if not isinstance(provider_id, str) or not isinstance(models, dict):
                continue
            for model_id, model in models.items():
                if not isinstance(model_id, str):
                    continue
                limit = self._context_limit_from(model)
                if limit is not None:
                    self._model_context_limits[(provider_id, model_id)] = limit
                    if provider_id == self._runtime_info.get("provider") and model_id == self._runtime_info.get("model"):
                        self._runtime_info["context_limit"] = limit
                        self._runtime_context_limit_key = (provider_id, model_id)

    def _load_model_metadata(self) -> None:
        provider = self._runtime_info.get("provider")
        model = self._runtime_info.get("model")
        key = (provider, model)
        if not provider or not model or key in self._model_metadata_loaded:
            return
        try:
            self._remember_provider_catalog(self._request("GET", "/provider"))
        except MemError:
            # Older or unavailable servers retain the configured fallback.
            pass
        finally:
            self._model_metadata_loaded.add(key)

    def _remember_context_limit(self, value: Any) -> None:
        limit = self._context_limit_from(value)
        if limit is None:
            return
        provider = self._runtime_info.get("provider")
        model = self._runtime_info.get("model")
        self._model_context_limits[(provider, model)] = limit
        self._runtime_info["context_limit"] = limit
        self._runtime_context_limit_key = (provider, model)

    @staticmethod
    def _numeric(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None

    @classmethod
    def _usage_tokens(cls, value: Any) -> float | None:
        if not isinstance(value, dict):
            return None
        total = cls._numeric(value.get("total"))
        if total is not None:
            return total
        fields = [cls._numeric(value.get("input")), cls._numeric(value.get("output"))]
        cache = value.get("cache")
        if isinstance(cache, dict):
            fields.extend([cls._numeric(cache.get("read")), cls._numeric(cache.get("write"))])
        else:
            fields.extend([cls._numeric(value.get("cache.read")), cls._numeric(value.get("cache.write"))])
        usable = [field for field in fields if field is not None]
        return sum(usable) if usable else None

    @staticmethod
    def _text_tokens(item: dict[str, Any]) -> int:
        return sum(
            len(str(part.get("text", ""))) // 4
            for part in item.get("parts", [])
            if part.get("type") == "text" and not part.get("synthetic", False)
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        params = dict(kwargs.pop("params", {}))
        if path.startswith("/session") or path.startswith("/tui") or path == "/provider":
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
            self._remember_context_limit(self._request("GET", f"/session/{self.session_id}"))
            return self.session_id
        session = self._request("POST", "/session", json={"title": "PCO 主会话"})
        self._remember_context_limit(session)
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
            self._set_model_identity(info.get("providerID"), info.get("modelID"))
            self._remember_context_limit(info)
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
        response = self._request("GET", f"/api/session/{self.session_id}/context")
        items = response.get("data", []) if isinstance(response, dict) else []
        assistant_indexes = [
            index
            for index, item in enumerate(items)
            if isinstance(item, dict) and item.get("info", item).get("role") == "assistant"
        ]
        if not assistant_indexes:
            # Older OpenCode fixtures omitted role while still returning usage.
            assistant_indexes = [
                index
                for index, item in enumerate(items)
                if isinstance(item, dict) and isinstance(item.get("info", item).get("tokens"), dict)
            ]
        latest_index = assistant_indexes[-1] if assistant_indexes else None
        if latest_index is not None:
            latest_info = items[latest_index].get("info", items[latest_index])
            if latest_info.get("providerID") or latest_info.get("modelID"):
                self._set_model_identity(latest_info.get("providerID"), latest_info.get("modelID"))
        current_model_key = self._clear_stale_runtime_context_limit()
        # A context response may carry the active model's limit. Associate it
        # only after the active model has been identified; never let it remain
        # as an unscoped runtime fallback after a model switch.
        self._remember_context_limit(response)
        self._load_model_metadata()
        used: float
        if latest_index is not None:
            latest = items[latest_index]
            latest_info = latest.get("info", latest)
            usage = self._usage_tokens(latest_info.get("tokens"))
        else:
            usage = None
        if usage is None:
            used = sum(self._text_tokens(item) for item in items if isinstance(item, dict))
        else:
            used = usage
            for item in items[latest_index + 1 :] if latest_index is not None else []:
                if isinstance(item, dict) and item.get("info", item).get("role") == "user":
                    used += self._text_tokens(item)
        limit = self._model_context_limits.get(current_model_key) or self.model_context_tokens
        ratio = used / float(limit) if limit else 0.0
        return max(0.0, min(1.0, ratio))

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
            result_urls = extract_result_urls(
                tool_name,
                tool_input,
                tool_output,
                status=str(state.get("status", "")),
                error=state.get("error"),
            )
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
                        "result_urls": result_urls,
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

    def _active_checkpoint_binding(self) -> tuple[str, str]:
        path = self.state_root / "active-checkpoint.json"
        ensure(path.is_file(), "NATIVE_COMPACT_CHECKPOINT_MISSING", "native_compact", "No durable checkpoint is available for native compact")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemError("NATIVE_COMPACT_CHECKPOINT_INVALID", "native_compact", "Durable checkpoint state is unreadable", retryable=True) from exc
        checkpoint_id = value.get("id") if isinstance(value, dict) else None
        ensure(isinstance(checkpoint_id, str) and checkpoint_id, "NATIVE_COMPACT_CHECKPOINT_INVALID", "native_compact", "Durable checkpoint has no ID")
        attempt_id = value.get("native_compact_attempt_id") if isinstance(value, dict) else None
        ensure(
            isinstance(attempt_id, str) and attempt_id,
            "NATIVE_COMPACT_ATTEMPT_MISSING",
            "native_compact",
            "Durable NATIVE_COMPACT state has no attempt ID",
        )
        return checkpoint_id, attempt_id

    def _native_bypass_path(self) -> Path:
        return self.state_root / "native-compact-bypass.json"

    def _persist_native_bypass(self, bypass: NativeCompactBypass) -> None:
        path = self._native_bypass_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(bypass.as_dict(), sort_keys=True), encoding="utf-8")
        temporary.replace(path)

    def _retire_native_bypass(self, bypass: NativeCompactBypass) -> None:
        path = self._native_bypass_path()
        if not path.exists():
            return
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return
        if not isinstance(stored, dict) or stored.get("token") == bypass.token:
            path.unlink(missing_ok=True)

    def _bind_native_bypass(self, value: NativeCompactBypass | dict[str, Any] | None) -> NativeCompactBypass:
        ensure(value is not None, "NATIVE_COMPACT_TOKEN_REQUIRED", "native_compact", "PCO native compact requires a one-shot bypass token")
        bypass = NativeCompactBypass.from_value(value)
        ensure(bypass.session_id == self.session_id, "NATIVE_COMPACT_TOKEN_SESSION_MISMATCH", "native_compact", "Native compact token is bound to another session")
        ensure(bypass.expires_at > int(time.time() * 1000), "NATIVE_COMPACT_TOKEN_EXPIRED", "native_compact", "Native compact bypass token has expired")
        checkpoint_id, attempt_id = self._active_checkpoint_binding()
        ensure(
            bypass.checkpoint_id in {"pending", ""} or bypass.checkpoint_id == checkpoint_id,
            "NATIVE_COMPACT_TOKEN_CHECKPOINT_MISMATCH",
            "native_compact",
            "Native compact token is bound to another checkpoint",
        )
        ensure(
            bypass.attempt_id == attempt_id,
            "NATIVE_COMPACT_TOKEN_ATTEMPT_MISMATCH",
            "native_compact",
            "Native compact token is bound to another attempt",
        )
        bound = NativeCompactBypass(
            token=bypass.token,
            checkpoint_id=checkpoint_id,
            session_id=bypass.session_id,
            attempt_id=attempt_id,
            expires_at=bypass.expires_at,
        )
        path = self._native_bypass_path()
        if path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MemError("NATIVE_COMPACT_TOKEN_INVALID", "native_compact", "Existing native compact token is unreadable", retryable=False) from exc
            ensure(
                isinstance(stored, dict) and stored.get("token") == bypass.token,
                "NATIVE_COMPACT_TOKEN_REPLAY",
                "native_compact",
                "A different native compact token is already pending",
            )
        self._persist_native_bypass(bound)
        return bound

    def _mint_native_bypass(self) -> NativeCompactBypass:
        """Create and durably bind the one-shot gate for the current phase.

        The checkpoint finalizer persists ``native_compact_attempt_id`` before
        calling the adapter.  The adapter is therefore the first component
        that is allowed to mint the private token used by the OpenCode hook.
        """

        checkpoint_id, attempt_id = self._active_checkpoint_binding()
        path = self._native_bypass_path()
        ensure(
            not path.exists(),
            "NATIVE_COMPACT_TOKEN_REPLAY",
            "native_compact",
            "A native compact bypass token is already pending",
        )
        token = NativeCompactBypass(
            token=secrets.token_urlsafe(32),
            checkpoint_id=checkpoint_id,
            session_id=self.session_id or "",
            attempt_id=attempt_id,
            expires_at=int(time.time() * 1000) + 5 * 60_000,
        )
        self._persist_native_bypass(token)
        return token

    def compact(self, bypass: NativeCompactBypass | dict[str, Any] | None = None) -> None:
        ensure(self.session_id is not None, "HARNESS_NOT_ATTACHED", "harness", "OpenCode session is not attached")
        supplied = bypass if bypass is not None else self.native_compact_bypass
        bound: NativeCompactBypass | None = None
        try:
            # Explicit values remain supported for adapter-level compatibility,
            # but the normal durable checkpoint path mints the token here.
            bound = self._bind_native_bypass(supplied) if supplied is not None else self._mint_native_bypass()
            items = self._request("GET", f"/session/{self.session_id}/message", params={"limit": 10000})
        except Exception:
            if bound is not None:
                self._retire_native_bypass(bound)
            raise
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
            if bound is not None:
                self._retire_native_bypass(bound)
            return

        try:
            ensure(provider_id is not None and model_id is not None, "HARNESS_MODEL_UNKNOWN", "harness", "Cannot compact without an OpenCode provider/model")
            result = self._request(
                "POST",
                f"/session/{self.session_id}/summarize",
                json={
                    "providerID": provider_id,
                    "modelID": model_id,
                    "auto": False,
                    "pco_native_compact": {
                        "token": bound.token,
                        "checkpoint_id": bound.checkpoint_id,
                        "session_id": bound.session_id,
                        "attempt_id": bound.attempt_id,
                        "expires_at": bound.expires_at,
                    },
                },
            )
            ensure(result is True, "HARNESS_COMPACTION_FAILED", "harness", "OpenCode did not confirm native compaction")
            ensure(
                not self._native_bypass_path().exists(),
                "NATIVE_COMPACT_GATE_NOT_CONSUMED",
                "native_compact",
                "OpenCode did not consume the bound PCO native compact token",
                retryable=True,
            )
        except Exception:
            # A failed, timed-out, or unconsumed attempt may never be replayed.
            self._retire_native_bypass(bound)
            raise

    def publish_context(self, bundle: dict[str, Any]) -> None:
        target = self.state_root / "context" / "bundle.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _receipt_inbox(self) -> Path:
        path = self.state_root / "receipt-inbox"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def publish_receipt(self, receipt: dict[str, Any], outbox: dict[str, Any]) -> dict[str, Any]:
        """Publish into the Plugin's durable, keyed receipt inbox.

        OpenCode 1.17's toast endpoint has no idempotency or lookup contract,
        so it is deliberately not used for final receipts. The inbox file is
        the Host resource: the Plugin consumes it by key and persists its own
        dedup/supersede index across restarts.
        """

        key = str(outbox.get("receipt_key") or "")
        payload_hash = str(outbox.get("payload_hash") or receipt_payload_hash(receipt))
        generation = int(outbox.get("generation", receipt.get("receipt_generation", 0)))
        ensure(key, "RECEIPT_KEY_INVALID", "receipt", "Receipt publish requires a stable receipt key")
        path = self._receipt_inbox() / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")
        resource_id = f"opencode_receipt_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MemError("RECEIPT_INBOX_CORRUPT", "receipt", "Durable Host receipt inbox entry is unreadable", retryable=True) from exc
            ensure(
                existing.get("payload_hash") == payload_hash,
                "RECEIPT_KEY_CONFLICT",
                "receipt",
                "Receipt key was reused with a different payload",
                retryable=False,
            )
            return {
                "host_resource_id": existing.get("host_resource_id", resource_id),
                "key": key,
                "generation": int(existing.get("generation", generation)),
                "payload_hash": payload_hash,
                "disposition": "existing",
            }
        record = {
            "host_resource_id": resource_id,
            "key": key,
            "generation": generation,
            "payload_hash": payload_hash,
            "supersedes_key": outbox.get("supersedes_key"),
            "payload": receipt,
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
        return {
            "host_resource_id": resource_id,
            "key": key,
            "generation": generation,
            "payload_hash": payload_hash,
            "disposition": "created",
        }

    def insert_receipt(self, receipt: dict[str, Any]) -> None:
        # Compatibility alias. Final checkpoint publication always goes
        # through the durable keyed inbox above; never fall back to toast.
        key = str(receipt.get("receipt_key") or f"legacy:{uuid.uuid4().hex}")
        outbox = {"receipt_key": key, "generation": int(receipt.get("receipt_generation", 0)), "payload_hash": receipt_payload_hash(receipt)}
        self.publish_receipt(receipt, outbox)
