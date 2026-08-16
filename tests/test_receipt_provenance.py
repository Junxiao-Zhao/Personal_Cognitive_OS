from __future__ import annotations

from pathlib import Path

from pco.harness import extract_result_urls, normalize_external_url, receipt_result_urls
from pco.validation import validate_profile

from conftest import NOW, envelope


def _receipt(receipt_id: str, payload: dict) -> dict:
    return envelope(receipt_id, "pco/search-receipt/v1", payload)


def _concept(url: str, receipt_id: str) -> dict:
    return envelope(
        "psy_provenance",
        "pco/psychology/v1",
        {
            "name": "引用 provenance",
            "description": "测试外部引用 provenance。",
            "aliases": [],
            "external_refs": [
                {"url": url, "title": "Reference", "accessed_at": NOW, "search_receipt": receipt_id}
            ],
            "status": "active",
        },
    )


def _problems(concept: dict, receipt: dict) -> list[dict]:
    records = {"psychologies": [concept], "search_receipts": [receipt]}
    return list(validate_profile(Path("."), records))


def test_websearch_extracts_only_urls_from_tool_output() -> None:
    assert extract_result_urls(
        "websearch",
        {"query": "https://input.example/proves-nothing"},
        "Result: https://Example.org/result#section.",
        status="completed",
    ) == ["https://example.org/result"]
    assert extract_result_urls(
        "websearch",
        {"query": "https://input.example/proves-nothing"},
        "No matching result",
        status="completed",
    ) == []


def test_webfetch_binds_target_only_after_successful_completion() -> None:
    target = "HTTPS://Example.org:443/article#section"
    assert extract_result_urls("webfetch", {"url": target}, "error", status="failed") == []
    assert extract_result_urls("webfetch", {"url": target}, "article body", status="completed") == [
        "https://example.org/article"
    ]
    assert normalize_external_url(target) == "https://example.org/article"


def test_external_reference_requires_exact_normalized_result_url() -> None:
    receipt = _receipt(
        "search_exact",
        {
            "worker_session_id": "ses_worker",
            "call_id": "call_search",
            "tool": "websearch",
            "input": {"query": "reference"},
            "output_excerpt": "The payload mentions https://example.org/not-the-result.",
            "result_urls": ["https://example.org/result"],
            "status": "completed",
        },
    )
    assert _problems(_concept("HTTPS://EXAMPLE.ORG:443/result#fragment", "search_exact"), receipt) == []
    problems = _problems(_concept("https://example.org/not-the-result", "search_exact"), receipt)
    assert [problem["code"] for problem in problems] == ["EXTERNAL_REFERENCE_INVALID"]


def test_historical_v1_without_result_urls_is_not_silently_upgraded() -> None:
    receipt = _receipt(
        "search_legacy",
        {
            "worker_session_id": "ses_worker",
            "call_id": "call_legacy",
            "tool": "websearch",
            "input": {"query": "reference"},
            "output_excerpt": "Result: https://example.org/legacy",
            "status": "completed",
        },
    )
    assert _problems(_concept("https://example.org/legacy", "search_legacy"), receipt)[0]["code"] == (
        "EXTERNAL_REFERENCE_INVALID"
    )


def test_incoming_legacy_fixture_can_be_explicitly_enriched_without_version_rewrite() -> None:
    receipt = _receipt(
        "search_fixture",
        {
            "worker_session_id": "ses_worker",
            "call_id": "call_fixture",
            "tool": "websearch",
            "input": {"query": "reference"},
            "output_excerpt": "Result: https://example.org/legacy",
            "status": "completed",
        },
    )
    assert receipt_result_urls(receipt) == ["https://example.org/legacy"]
    assert receipt["schema_version"] == "pco/search-receipt/v1"
