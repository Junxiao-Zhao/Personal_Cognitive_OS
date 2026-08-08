from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from mem_core.models import latest_by_id


def _problem(
    code: str,
    message: str,
    *,
    stream: str,
    record_id: str,
    path: str,
    value: Any = None,
    recovery: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "phase": "profile_validation",
        "message": message,
        "stream": stream,
        "record_id": record_id,
        "path": path,
        "value": value,
        "retryable": True,
        "recovery": recovery or [],
    }


def validate_profile(
    _repo_root: Path,
    records: dict[str, list[dict[str, Any]]],
) -> Iterable[dict[str, Any]]:
    current = {stream: latest_by_id(items) for stream, items in records.items()}
    messages = current.get("messages", {})
    sources = current.get("sources", {})
    search_receipts = current.get("search_receipts", {})
    link_targets = {
        "psychologies": current.get("psychologies", {}),
        "philosophies": current.get("philosophies", {}),
        "archetypes": current.get("archetypes", {}),
    }
    structured_evidence = {
        stream: current.get(stream, {})
        for stream in ("events", "psychologies", "philosophies", "archetypes", "hypotheses")
    }
    evidence_prefixes = {
        "event": "events",
        "events": "events",
        "psychology": "psychologies",
        "psychologies": "psychologies",
        "philosophy": "philosophies",
        "philosophies": "philosophies",
        "archetype": "archetypes",
        "archetypes": "archetypes",
        "hypothesis": "hypotheses",
        "hypotheses": "hypotheses",
    }

    for concept_stream in ("psychologies", "philosophies"):
        for record in records.get(concept_stream, []):
            refs = record["payload"].get("external_refs", [])
            if not refs:
                yield _problem(
                    "EXTERNAL_REFERENCE_REQUIRED",
                    "Psychology and philosophy concepts require an external reference",
                    stream=concept_stream,
                    record_id=record["id"],
                    path="/payload/external_refs",
                    recovery=["Search a reliable external source and save its search receipt"],
                )
            for index, ref in enumerate(refs):
                parsed = urlparse(ref.get("url", ""))
                receipt_id = ref.get("search_receipt")
                receipt = search_receipts.get(receipt_id) if isinstance(receipt_id, str) else None
                receipt_text = str(receipt.get("payload", {})) if receipt else ""
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.netloc
                    or receipt is None
                    or receipt["payload"].get("status") != "completed"
                    or ref.get("url", "") not in receipt_text
                ):
                    yield _problem(
                        "EXTERNAL_REFERENCE_INVALID",
                        "External references require an HTTP(S) URL bound to a completed wrapper-captured search receipt",
                        stream=concept_stream,
                        record_id=record["id"],
                        path=f"/payload/external_refs/{index}",
                        value=ref,
                    )

    for record in records.get("events", []):
        for target_stream, ids in record["payload"].get("links", {}).items():
            for index, target_id in enumerate(ids):
                if target_id not in link_targets.get(target_stream, {}):
                    yield _problem(
                        "REFERENCE_NOT_FOUND",
                        f"Referenced {target_stream} record does not exist: {target_id}",
                        stream="events",
                        record_id=record["id"],
                        path=f"/payload/links/{target_stream}/{index}",
                        value=target_id,
                        recovery=[f"Create {target_id}", "Remove the reference", "Replace it with an existing ID"],
                    )

    evidence_streams = ("events", "archetypes", "hypotheses", "meta_revisions")
    for stream in evidence_streams:
        for record in records.get(stream, []):
            fields = ["evidence_refs"]
            if stream == "hypotheses":
                fields.append("counter_evidence_refs")
            for field in fields:
                for index, ref in enumerate(record["payload"].get(field, [])):
                    if ref.startswith("message:"):
                        message_id = ref.split(":", 1)[1]
                        message = messages.get(message_id)
                        if message is None:
                            yield _problem(
                                "EVIDENCE_NOT_FOUND",
                                f"Message evidence does not exist: {message_id}",
                                stream=stream,
                                record_id=record["id"],
                                path=f"/payload/{field}/{index}",
                                value=ref,
                            )
                        elif message["payload"]["role"] != "user":
                            yield _problem(
                                "EVIDENCE_INELIGIBLE",
                                "Assistant messages cannot serve as direct user evidence",
                                stream=stream,
                                record_id=record["id"],
                                path=f"/payload/{field}/{index}",
                                value=ref,
                                recovery=["Cite a user message, event, or registered source instead"],
                            )
                    elif ref.startswith("source:"):
                        source_locator = ref.split(":", 1)[1]
                        source_id = source_locator.split("#", 1)[0]
                        if source_id not in sources:
                            yield _problem(
                                "EVIDENCE_NOT_FOUND",
                                f"Source evidence does not exist: {source_id}",
                                stream=stream,
                                record_id=record["id"],
                                path=f"/payload/{field}/{index}",
                                value=ref,
                            )
                    else:
                        prefix, separator, candidate_id = ref.partition(":")
                        if separator:
                            target_stream = evidence_prefixes.get(prefix)
                            matches = [target_stream] if target_stream and candidate_id in structured_evidence[target_stream] else []
                        else:
                            candidate_id = ref
                            matches = [name for name, items in structured_evidence.items() if candidate_id in items]
                        if len(matches) != 1:
                            code = "EVIDENCE_REFERENCE_INVALID" if not matches else "EVIDENCE_REFERENCE_AMBIGUOUS"
                            yield _problem(
                                code,
                                f"Structured evidence reference must resolve to exactly one record: {ref}",
                                stream=stream,
                                record_id=record["id"],
                                path=f"/payload/{field}/{index}",
                                value=ref,
                                recovery=["Use an existing entity ID or a supported qualified reference"],
                            )

    for record in records.get("meta_revisions", []):
        previous = record["payload"].get("previous_revision")
        expected = None if record["revision"] == 1 else f"meta_current@{record['revision'] - 1}"
        if previous != expected:
            yield _problem(
                "META_PREVIOUS_REVISION_INVALID",
                f"Expected previous_revision {expected!r}",
                stream="meta_revisions",
                record_id=record["id"],
                path="/payload/previous_revision",
                value=previous,
            )
        for index, hypothesis_id in enumerate(record["payload"].get("promotion_refs", [])):
            if hypothesis_id not in current.get("hypotheses", {}):
                yield _problem(
                    "REFERENCE_NOT_FOUND",
                    f"Promotion hypothesis does not exist: {hypothesis_id}",
                    stream="meta_revisions",
                    record_id=record["id"],
                    path=f"/payload/promotion_refs/{index}",
                    value=hypothesis_id,
                )
