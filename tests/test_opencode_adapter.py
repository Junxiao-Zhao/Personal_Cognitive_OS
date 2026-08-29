from __future__ import annotations

import json

import httpx
import pytest
from mem_core.errors import MemError

from pco.harness import OpenCodeAdapter, WorkerHandle, receipt_payload_hash

from conftest import continuation


def native_bypass(workspace, *, checkpoint_id: str = "ckpt_native", token: str = "token-native") -> dict[str, object]:
    workspace.save_json("active-checkpoint.json", {"id": checkpoint_id, "native_compact_attempt_id": "attempt-native"})
    return {
        "token": token,
        "checkpoint_id": checkpoint_id,
        "session_id": "ses_main",
        "attempt_id": "attempt-native",
        "expires_at": 9_999_999_999_999,
    }


def test_opencode_117_http_contract_and_worker_reclamation(workspace, tmp_path) -> None:
    requests: list[tuple[str, str, dict]] = []

    def response(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/global/health":
            return httpx.Response(200, json={"healthy": True, "version": "1.17.18"})
        if path == "/session/ses_main" and request.method == "GET":
            return httpx.Response(200, json={"id": "ses_main"})
        if path == "/session/ses_main/message":
            return httpx.Response(
                200,
                json=[
                    {
                        "info": {"id": "msg_native_user", "role": "user", "time": {"created": 1785686400000}},
                        "parts": [{"type": "text", "text": "用户公开消息"}],
                    },
                    {
                        "info": {
                            "id": "msg_native_assistant",
                            "role": "assistant",
                            "providerID": "fixture-provider",
                            "modelID": "fixture-model",
                            "time": {"created": 1785686401000},
                        },
                        "parts": [
                            {"type": "reasoning", "text": "exposed"},
                            {"type": "text", "text": "assistant 公开消息"},
                        ],
                    },
                    {
                        "info": {"id": "msg_native_control", "role": "user", "time": {"created": 1785686402000}},
                        "parts": [
                            {
                                "type": "text",
                                "text": "[PCO_CONTROL] Call pco_checkpoint exactly once.",
                                "metadata": {"pco_control": True},
                            }
                        ],
                    },
                ],
            )
        if path == "/api/session/ses_main/context":
            return httpx.Response(200, json={"data": [{"info": {"tokens": {"input": 3200}}, "parts": []}]})
        if path == "/provider":
            return httpx.Response(200, json={"all": [{"id": "fixture-provider", "models": {"fixture-model": {"limit": {"context": 6400}}}}]})
        if path == "/session" and request.method == "POST":
            return httpx.Response(200, json={"id": "ses_child"})
        if path == "/session/ses_child/message":
            worker_payload = {
                "operations": [{"op": "append", "stream": "continuations", "record": continuation()}],
                "diagnostics": [],
                "skill_versions": {"pco-memory": "0.1.0"},
            }
            return httpx.Response(
                200,
                json={
                    "info": {
                        "providerID": "worker-provider",
                        "modelID": "worker-model",
                        "structured": worker_payload,
                    },
                    "parts": [
                        {
                            "type": "tool",
                            "callID": "call_search_1",
                            "tool": "websearch",
                            "state": {
                                "status": "completed",
                                "input": {"query": "evaluation concern"},
                                "output": "https://example.org/reference",
                            },
                        }
                    ],
                },
            )
        if path == "/session/ses_main/summarize":
            (workspace.config.state_root / "native-compact-bypass.json").unlink(missing_ok=True)
            return httpx.Response(200, json=True)
        if path in {
            "/session/ses_child/abort",
            "/session/ses_child",
            "/tui/show-toast",
        }:
            return httpx.Response(200, json=True)
        raise AssertionError(f"Unexpected OpenCode request: {request.method} {path}")

    adapter = OpenCodeAdapter(
        base_url="http://127.0.0.1:4096",
        directory=tmp_path,
        state_root=workspace.config.state_root,
        session_id="ses_main",
        model_context_tokens=6400,
    )
    adapter.client.close()
    adapter.client = httpx.Client(base_url="http://127.0.0.1:4096", transport=httpx.MockTransport(response))

    assert adapter.attach_or_create() == "ses_main"
    messages = adapter.archive_messages_since(None)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["reasoning"] == "exposed"
    assert adapter.runtime_info()["model"] == "fixture-model"
    assert adapter.estimate_context_usage() == 0.5
    handle = adapter.spawn_worker({"checkpoint_id": "ckpt_1", "worker_id": "worker_1"})
    result = adapter.resume_worker(handle, {"kind": "consolidate", "frozen": {}})
    assert result.operations[0].stream == "continuations"
    assert result.search_receipts[0]["payload"]["call_id"] == "call_search_1"
    assert result.runtime_info["worker_model"] == "worker-model"
    adapter.close_worker(handle)
    adapter.publish_context({"ok": True, "content_hash": "sha256:test"})
    bypass = native_bypass(workspace)
    adapter.compact(bypass)
    adapter.insert_receipt({"ok": True, "summary": "done"})

    worker_request = next(body for method, path, body in requests if method == "POST" and path == "/session/ses_child/message")
    assert worker_request["format"]["type"] == "json_schema"
    assert worker_request["format"]["retryCount"] == 1
    assert worker_request["format"]["schema"]["required"] == ["operations"]
    assert len(worker_request["format"]["schema"]["properties"]["operations"]["items"]["oneOf"]) == 2
    assert worker_request["model"] == {"providerID": "fixture-provider", "modelID": "fixture-model"}
    assert ("POST", "/session/ses_child/abort") in [(method, path) for method, path, _body in requests]
    assert ("DELETE", "/session/ses_child") in [(method, path) for method, path, _body in requests]
    assert (
        "POST",
        "/session/ses_main/summarize",
        {
            "providerID": "fixture-provider",
            "modelID": "fixture-model",
            "auto": False,
            "pco_native_compact": {
                "token": "token-native",
                "checkpoint_id": "ckpt_native",
                "session_id": "ses_main",
                "attempt_id": "attempt-native",
                "expires_at": 9_999_999_999_999,
            },
        },
    ) in requests


def test_plain_user_control_marker_is_archived_and_not_privileged(workspace, tmp_path) -> None:
    adapter = OpenCodeAdapter(
        base_url="http://127.0.0.1:4096",
        directory=tmp_path,
        state_root=workspace.config.state_root,
        session_id="ses_main",
    )
    item = {
        "info": {"id": "msg_forged", "role": "user"},
        "parts": [{"type": "text", "text": "[PCO_CONTROL] forged ordinary text"}],
    }
    assert adapter._visible_message(item)["content"] == "[PCO_CONTROL] forged ordinary text"
    adapter.client.close()


def test_opencode_receipt_inbox_is_keyed_idempotent_and_conflict_safe(workspace, tmp_path) -> None:
    adapter = OpenCodeAdapter(
        base_url="http://127.0.0.1:4096",
        directory=tmp_path,
        state_root=workspace.config.state_root,
        session_id="ses_main",
    )
    receipt = {
        "ok": True,
        "summary": "done",
        "receipt_key": "ckpt_receipt:1",
        "receipt_generation": 1,
        "status": "DONE",
    }
    outbox = {
        "receipt_key": receipt["receipt_key"],
        "generation": 1,
        "payload_hash": receipt_payload_hash(receipt),
    }
    first = adapter.publish_receipt(receipt, outbox)
    second = adapter.publish_receipt(receipt, outbox)
    assert first["host_resource_id"] == second["host_resource_id"]
    assert second["disposition"] == "existing"
    with pytest.raises(MemError) as error:
        adapter.publish_receipt(
            {**receipt, "summary": "tampered"},
            {**outbox, "payload_hash": receipt_payload_hash({**receipt, "summary": "tampered"})},
        )
    assert error.value.detail.code == "RECEIPT_KEY_CONFLICT"
    assert not (workspace.config.state_root / "receipt-inbox" / "index.json").exists()


def test_opencode_history_paginates_back_to_archive_cursor(workspace, tmp_path) -> None:
    def message(index: int) -> dict:
        return {
            "info": {"id": f"msg_{index:04d}", "role": "user", "time": {"created": 1785686400000 + index}},
            "parts": [{"type": "text", "text": f"turn {index}"}],
        }

    def response(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session/ses_main/message"
        before = request.url.params.get("before")
        if before is None:
            return httpx.Response(200, json=[message(index) for index in range(1000, 2000)])
        assert before == "msg_1000"
        return httpx.Response(200, json=[message(index) for index in range(500, 1000)])

    adapter = OpenCodeAdapter(
        base_url="http://127.0.0.1:4096",
        directory=tmp_path,
        state_root=workspace.config.state_root,
        session_id="ses_main",
    )
    adapter.client.close()
    adapter.client = httpx.Client(base_url="http://127.0.0.1:4096", transport=httpx.MockTransport(response))
    messages = adapter.archive_messages_since("msg_0500")
    assert messages[0]["native_message_id"] == "msg_0501"
    assert messages[-1]["native_message_id"] == "msg_1999"
    assert len(messages) == 1499
    adapter.client.close()


def test_compaction_retry_detects_completed_native_summary(workspace, tmp_path) -> None:
    requests: list[tuple[str, str]] = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/session/ses_main/message":
            return httpx.Response(
                200,
                json=[
                    {
                        "info": {"id": "msg_user", "role": "user"},
                        "parts": [{"type": "text", "text": "preserved raw turn"}],
                    },
                    {
                        "info": {
                            "id": "msg_assistant",
                            "role": "assistant",
                            "providerID": "fixture-provider",
                            "modelID": "fixture-model",
                        },
                        "parts": [{"type": "text", "text": "preserved raw reply"}],
                    },
                    {
                        "info": {"id": "msg_compact", "role": "user"},
                        "parts": [{"type": "compaction", "auto": False}],
                    },
                    {
                        "info": {
                            "id": "msg_summary",
                            "role": "assistant",
                            "summary": True,
                            "providerID": "fixture-provider",
                            "modelID": "fixture-model",
                        },
                        "parts": [{"type": "text", "text": "native summary"}],
                    },
                ],
            )
        raise AssertionError(f"Unexpected OpenCode request: {request.method} {request.url.path}")

    adapter = OpenCodeAdapter(
        base_url="http://127.0.0.1:4096",
        directory=tmp_path,
        state_root=workspace.config.state_root,
        session_id="ses_main",
    )
    adapter.client.close()
    adapter.client = httpx.Client(base_url="http://127.0.0.1:4096", transport=httpx.MockTransport(response))

    assert [item["content"] for item in adapter.archive_messages_since(None)] == [
        "preserved raw turn",
        "preserved raw reply",
    ]
    adapter.compact(native_bypass(workspace))
    assert ("POST", "/session/ses_main/summarize") not in requests


def test_compaction_marker_without_summary_is_not_complete(workspace, tmp_path) -> None:
    requests: list[tuple[str, str]] = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/session/ses_main/message":
            return httpx.Response(
                200,
                json=[
                    {
                        "info": {"id": "msg_user", "role": "user"},
                        "parts": [{"type": "text", "text": "turn"}],
                    },
                    {
                        "info": {
                            "id": "msg_assistant",
                            "role": "assistant",
                            "providerID": "fixture-provider",
                            "modelID": "fixture-model",
                        },
                        "parts": [{"type": "text", "text": "reply"}],
                    },
                    {
                        "info": {"id": "msg_compact", "role": "user"},
                        "parts": [{"type": "compaction", "auto": False}],
                    },
                ],
            )
        if request.url.path == "/session/ses_main/summarize":
            (workspace.config.state_root / "native-compact-bypass.json").unlink(missing_ok=True)
            return httpx.Response(200, json=True)
        raise AssertionError(f"Unexpected OpenCode request: {request.method} {request.url.path}")

    adapter = OpenCodeAdapter(
        base_url="http://127.0.0.1:4096",
        directory=tmp_path,
        state_root=workspace.config.state_root,
        session_id="ses_main",
    )
    adapter.client.close()
    adapter.client = httpx.Client(base_url="http://127.0.0.1:4096", transport=httpx.MockTransport(response))

    adapter.compact(native_bypass(workspace, token="token-marker"))
    assert ("POST", "/session/ses_main/summarize") in requests


def test_native_compact_requires_hook_consumption_and_retires_failed_token(workspace, tmp_path) -> None:
    requests: list[dict] = []

    def response(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append(body)
        if request.url.path == "/session/ses_main/message":
            return httpx.Response(200, json=[
                {"info": {"id": "msg_user", "role": "user"}, "parts": [{"type": "text", "text": "turn"}]},
                {"info": {"id": "msg_assistant", "role": "assistant", "providerID": "p", "modelID": "m"}, "parts": [{"type": "text", "text": "reply"}]},
            ])
        if request.url.path == "/session/ses_main/summarize":
            # Simulate a Host that returns success without invoking the PCO
            # compacting hook. The adapter must reject this response.
            return httpx.Response(200, json=True)
        raise AssertionError(f"Unexpected OpenCode request: {request.method} {request.url.path}")

    adapter = OpenCodeAdapter(
        base_url="http://127.0.0.1:4096",
        directory=tmp_path,
        state_root=workspace.config.state_root,
        session_id="ses_main",
    )
    adapter.client.close()
    adapter.client = httpx.Client(base_url="http://127.0.0.1:4096", transport=httpx.MockTransport(response))
    with pytest.raises(MemError) as error:
        adapter.compact(native_bypass(workspace, token="token-unconsumed"))
    assert error.value.detail.code == "NATIVE_COMPACT_GATE_NOT_CONSUMED"
    assert not (workspace.config.state_root / "native-compact-bypass.json").exists()
    assert requests[-1]["pco_native_compact"]["checkpoint_id"] == "ckpt_native"


def test_native_compact_rejects_wrong_session_binding_before_summarize(workspace, tmp_path) -> None:
    adapter = OpenCodeAdapter(
        base_url="http://127.0.0.1:4096",
        directory=tmp_path,
        state_root=workspace.config.state_root,
        session_id="ses_main",
    )
    with pytest.raises(MemError) as error:
        adapter.compact({
            **native_bypass(workspace, token="token-wrong-session"),
            "session_id": "ses_other",
        })
    assert error.value.detail.code == "NATIVE_COMPACT_TOKEN_SESSION_MISMATCH"


def test_native_compact_mints_python_token_from_durable_attempt(workspace, tmp_path) -> None:
    requests: list[dict] = []
    native_bypass(workspace)

    def response(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append(body)
        if request.url.path == "/session/ses_main/message":
            return httpx.Response(200, json=[
                {"info": {"id": "msg_user", "role": "user"}, "parts": [{"type": "text", "text": "turn"}]},
                {"info": {"id": "msg_assistant", "role": "assistant", "providerID": "p", "modelID": "m"}, "parts": [{"type": "text", "text": "reply"}]},
            ])
        if request.url.path == "/session/ses_main/summarize":
            persisted = json.loads((workspace.config.state_root / "native-compact-bypass.json").read_text())
            assert persisted["checkpointID"] == "ckpt_native"
            assert persisted["sessionID"] == "ses_main"
            assert persisted["attemptID"] == "attempt-native"
            assert body["pco_native_compact"]["token"] == persisted["token"]
            (workspace.config.state_root / "native-compact-bypass.json").unlink()
            return httpx.Response(200, json=True)
        raise AssertionError(f"Unexpected OpenCode request: {request.method} {request.url.path}")

    adapter = OpenCodeAdapter(
        base_url="http://127.0.0.1:4096",
        directory=tmp_path,
        state_root=workspace.config.state_root,
        session_id="ses_main",
    )
    adapter.client.close()
    adapter.client = httpx.Client(base_url="http://127.0.0.1:4096", transport=httpx.MockTransport(response))
    adapter.compact()
    assert requests[-1]["pco_native_compact"]["attempt_id"] == "attempt-native"
    assert not (workspace.config.state_root / "native-compact-bypass.json").exists()


def test_worker_falls_back_to_validated_json_text(workspace, tmp_path) -> None:
    requests: list[dict] = []
    worker_payload = {
        "operations": [{"op": "append", "stream": "continuations", "record": continuation()}],
        "diagnostics": [],
        "skill_versions": {"pco-memory": "0.1.0"},
    }

    def response(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/session/ses_child/message":
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    json={
                        "info": {
                            "providerID": "free-provider",
                            "modelID": "free-model",
                            "error": {"name": "StructuredOutputError", "data": {"message": "schema unsupported"}},
                        },
                        "parts": [],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "info": {"providerID": "free-provider", "modelID": "free-model"},
                    "parts": [
                        {
                            "type": "text",
                            "text": "```json\n" + json.dumps(worker_payload, ensure_ascii=False) + "\n```",
                        }
                    ],
                },
            )
        if request.url.path == "/session/ses_child/abort":
            return httpx.Response(200, json=True)
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    adapter = OpenCodeAdapter(
        base_url="http://127.0.0.1:4096",
        directory=tmp_path,
        state_root=workspace.config.state_root,
        session_id="ses_main",
    )
    adapter.client.close()
    adapter.client = httpx.Client(base_url="http://127.0.0.1:4096", transport=httpx.MockTransport(response))
    adapter._runtime_info.update({"provider": "free-provider", "model": "free-model"})

    result = adapter.resume_worker(
        WorkerHandle("worker_1", "native_subagent", "ses_child"),
        {"kind": "consolidate", "frozen": {}},
    )
    assert result.operations[0].stream == "continuations"
    assert result.runtime_info["worker_output_mode"] == "validated_json_text_fallback"
    assert requests[0]["format"]["type"] == "json_schema"
    assert "format" not in requests[1]
    assert requests[1]["model"] == {"providerID": "free-provider", "modelID": "free-model"}


def test_worker_corrects_invalid_fallback_json_once(workspace, tmp_path) -> None:
    requests: list[dict] = []
    worker_payload = {
        "operations": [{"op": "append", "stream": "continuations", "record": continuation()}],
        "diagnostics": [],
        "skill_versions": {"pco-memory": "0.1.0"},
    }

    def response(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/session/ses_child/message":
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    json={
                        "info": {"error": {"name": "StructuredOutputError", "data": {"message": "unsupported"}}},
                        "parts": [],
                    },
                )
            if len(requests) == 2:
                return httpx.Response(
                    200,
                    json={
                        "info": {"providerID": "free-provider", "modelID": "free-model"},
                        "parts": [{"type": "text", "text": "not JSON at all"}],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "info": {"providerID": "free-provider", "modelID": "free-model"},
                    "parts": [{"type": "text", "text": json.dumps(worker_payload, ensure_ascii=False)}],
                },
            )
        if request.url.path == "/session/ses_child/abort":
            return httpx.Response(200, json=True)
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    adapter = OpenCodeAdapter(
        base_url="http://127.0.0.1:4096",
        directory=tmp_path,
        state_root=workspace.config.state_root,
        session_id="ses_main",
    )
    adapter.client.close()
    adapter.client = httpx.Client(base_url="http://127.0.0.1:4096", transport=httpx.MockTransport(response))
    adapter._runtime_info.update({"provider": "free-provider", "model": "free-model"})

    result = adapter.resume_worker(
        WorkerHandle("worker_1", "native_subagent", "ses_child"),
        {"kind": "consolidate", "frozen": {}},
    )
    assert result.operations[0].stream == "continuations"
    assert result.runtime_info["worker_output_mode"] == "validated_json_text_correction"
    assert len(requests) == 3
    correction = json.loads(requests[2]["parts"][0]["text"])
    assert correction["kind"] == "json_syntax_correction"
    assert "neither structured output nor a JSON object" in correction["parse_error"]


def test_worker_repairs_json_syntax_before_semantic_validation() -> None:
    worker_payload = {
        "operations": [{"op": "append", "stream": "continuations", "record": continuation()}],
        "diagnostics": [],
    }
    malformed = json.dumps(worker_payload, ensure_ascii=False).replace('"operations"', "'operations'", 1)
    proposal, repaired = OpenCodeAdapter._worker_proposal(
        {
            "info": {"providerID": "free-provider", "modelID": "free-model"},
            "parts": [{"type": "text", "text": malformed}],
        },
        structured=False,
    )
    assert repaired
    assert proposal["operations"][0]["stream"] == "continuations"
