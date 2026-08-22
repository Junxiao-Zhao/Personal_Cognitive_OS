from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import pco.cli as cli
from mem_core.errors import MemError
from pco.harness import OpenCodeAdapter


def _adapter(workspace, response) -> OpenCodeAdapter:
    adapter = OpenCodeAdapter(
        base_url="http://127.0.0.1:4096",
        directory=workspace.root,
        state_root=workspace.config.state_root,
        session_id="ses_main",
        model_context_tokens=1000,
    )
    adapter.client.close()
    adapter.client = httpx.Client(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(response),
    )
    adapter._runtime_info.update({"provider": "fixture-provider", "model": "fixture-model"})
    return adapter


def test_source_add_cli_supports_allowlisted_fake_remote_and_is_idempotent(workspace, monkeypatch) -> None:
    calls: list[str] = []

    def fake_reader(locator: str) -> dict[str, object]:
        calls.append(locator)
        return {
            "locator": locator,
            "reader": "fake-remote",
            "normalized_content": "remote fixture\r\n",
            "media_type": "text/markdown",
            "read_metadata": {"credential": "must-not-enter-git"},
        }

    workspace.profile.registry.register("test.fake_remote", fake_reader)
    workspace.profile.raw["source_readers"] = {"fake-remote": "test.fake_remote"}
    monkeypatch.setattr(cli, "_workspace", lambda _args, init=False: workspace)

    args = cli.build_parser().parse_args(
        [
            "--workspace",
            str(workspace.root),
            "source",
            "add",
            "--locator",
            "fake://document/42",
            "--reader",
            "fake-remote",
            "--provider",
            "fixture",
            "--name",
            "Fixture",
        ]
    )
    first = cli.run(args)
    second = cli.run(args)
    assert first["ok"] and first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["source_id"] == first["source_id"]
    assert calls == []

    conflict_args = cli.build_parser().parse_args(
        [
            "--workspace",
            str(workspace.root),
            "source",
            "add",
            "--locator",
            "fake://document/42",
            "--reader",
            "fake-remote",
            "--provider",
            "other",
            "--name",
            "Fixture",
        ]
    )
    with pytest.raises(MemError) as error:
        cli.run(conflict_args)
    assert error.value.detail.code == "SOURCE_LOCATOR_CONFLICT"


def test_source_add_cli_keeps_local_path_entrypoint(workspace, monkeypatch, tmp_path: Path) -> None:
    source_path = tmp_path / "local.md"
    source_path.write_text("local\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_workspace", lambda _args, init=False: workspace)
    args = cli.build_parser().parse_args(["--workspace", str(workspace.root), "source", "add", str(source_path)])
    result = cli.run(args)
    assert result["ok"] and result["record"]["payload"]["reader_skill"] == "local-readonly"


def test_source_add_cli_rejects_unregistered_remote_reader(workspace, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_workspace", lambda _args, init=False: workspace)
    args = cli.build_parser().parse_args(
        [
            "--workspace",
            str(workspace.root),
            "source",
            "add",
            "--locator",
            "affine://workspace/document-id",
            "--reader",
            "affine-cli",
            "--provider",
            "affine",
        ]
    )
    with pytest.raises(MemError) as error:
        cli.run(args)
    assert error.value.detail.code == "SOURCE_READER_REQUIRED"


def test_context_usage_uses_latest_assistant_total_and_tail_user_text(workspace) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/provider":
            return httpx.Response(200, json={"all": [{"id": "fixture-provider", "models": {"fixture-model": {"limit": {"context": 400}}}}]})
        assert request.url.path == "/api/session/ses_main/context"
        return httpx.Response(
            200,
            json={
                "model": {"contextLength": 400},
                "data": [
                    {"info": {"role": "assistant", "tokens": {"total": 100}}, "parts": []},
                    {"info": {"role": "assistant", "tokens": {"total": 300}}, "parts": []},
                    {"info": {"role": "user"}, "parts": [{"type": "text", "text": "x" * 40}]},
                ],
            },
        )

    adapter = _adapter(workspace, response)
    assert adapter.estimate_context_usage() == 0.775
    adapter.client.close()


def test_context_usage_falls_back_to_cache_components_and_clamps(workspace) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/provider":
            return httpx.Response(200, json={"all": [{"id": "fixture-provider", "models": {"fixture-model": {"limit": {"context": 1000}}}}]})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "info": {
                            "role": "assistant",
                            "tokens": {"input": 100, "output": 20, "cache": {"read": 30, "write": 10}},
                        },
                        "parts": [],
                    }
                ]
            },
        )

    adapter = _adapter(workspace, response)
    assert adapter.estimate_context_usage() == 0.16
    adapter.client.close()


def test_context_usage_prefers_provider_model_context_limit(workspace) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/provider":
            return httpx.Response(200, json={"all": [{"id": "fixture-provider", "models": {"fixture-model": {"limit": {"context": 200}}}}]})
        return httpx.Response(
            200,
            json={
                "model": {"contextLength": 1000},
                "data": [{"info": {"role": "assistant", "tokens": {"total": 100}}, "parts": []}],
            },
        )

    adapter = _adapter(workspace, response)
    assert adapter.estimate_context_usage() == 0.5
    assert adapter.runtime_info()["context_limit"] == 200
    adapter.client.close()


def test_context_usage_drops_previous_model_limit_when_current_model_is_unknown(workspace) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/provider":
            return httpx.Response(200, json={"all": [{"id": "old-provider", "models": {"old-model": {"limit": {"context": 100}}}}]})
        return httpx.Response(
            200,
            json={
                "data": [{
                    "info": {
                        "role": "assistant",
                        "providerID": "new-provider",
                        "modelID": "new-model",
                        "tokens": {"total": 500},
                    },
                    "parts": [],
                }],
            },
        )

    adapter = _adapter(workspace, response)
    adapter._runtime_info.update({"provider": "old-provider", "model": "old-model", "context_limit": 100})
    adapter._runtime_context_limit_key = ("old-provider", "old-model")
    adapter._model_context_limits[("old-provider", "old-model")] = 100
    assert adapter.estimate_context_usage() == 0.5
    assert adapter.runtime_info()["provider"] == "new-provider"
    assert "context_limit" not in adapter.runtime_info()
    adapter.client.close()


def test_context_usage_without_usage_uses_visible_text_and_clamps(workspace) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/provider":
            return httpx.Response(200, json={"all": [{"id": "fixture-provider", "models": {"fixture-model": {"limit": {"context": 10}}}}]})
        return httpx.Response(
            200,
            json={"model": {"contextLength": 10}, "data": [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "x" * 80}]}]},
        )

    adapter = _adapter(workspace, response)
    assert adapter.estimate_context_usage() == 1.0
    adapter.client.close()


def test_plugin_uses_host_auto_marker_and_does_not_accept_agent_trigger() -> None:
    plugin = Path("packages/pco/src/pco/resources/opencode/plugins/pco.ts").read_text(encoding="utf-8")
    assert "ForegroundAutoMarker" in plugin
    assert "consumeForegroundAutoMarker(context.sessionID, contextMessageID)" in plugin
    assert '"--trigger", trigger' in plugin
    assert "issueForegroundAutoMarker(event.properties.sessionID)" in plugin
    assert "retireForegroundAutoMarker(marker.nonce, \"expired\")" in plugin
