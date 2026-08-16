from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mem_core.repository import MemoryRepository

from .repo_loader import repository_at, resolve_derivation_source_commit


_BACKLINK_STREAMS = ("events", "archetypes", "hypotheses", "meta_revisions")
_REFERENCE_FIELDS = ("evidence_refs", "counter_evidence_refs")
_RETIRED_STATUSES = {"disputed", "inactive", "rejected", "superseded", "tombstone"}


def _edges(record: dict[str, Any], stream: str) -> list[tuple[str, str]]:
    payload = record["payload"]
    result: list[tuple[str, str]] = []
    if stream == "events":
        for relation, targets in payload.get("links", {}).items():
            result.extend((target, relation) for target in targets)
    for relation in _REFERENCE_FIELDS:
        result.extend((target, relation) for target in payload.get(relation, []))
    if stream == "meta_revisions":
        result.extend((target, "promotion") for target in payload.get("promotion_refs", []))
    return result


def _is_current(record: dict[str, Any]) -> bool:
    return record["payload"].get("status") not in _RETIRED_STATUSES


def build(
    *,
    repo_root: Path,
    output_path: str | Path | None = None,
    source_commit: str | None = None,
    memory_commit: str | None = None,
    state_root: str | Path | None = None,
    **_: Any,
) -> dict[str, Any]:
    repo_root = Path(repo_root)
    source_commit = resolve_derivation_source_commit(
        Path(repo_root),
        state_root=Path(state_root) if state_root is not None else (Path(output_path).parent.parent if output_path else None),
        source_commit=source_commit or memory_commit,
    )
    with repository_at(repo_root, source_commit) as repository:
        records = repository.records_by_stream()
        current_backlinks: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}
        historical_backlinks: dict[str, list[dict[str, Any]]] = {}
        latest = {
            stream: repository.current_records(stream)
            for stream in _BACKLINK_STREAMS
        }

        def add(
            target: str,
            source_stream: str,
            source_id: str,
            relation: str,
            record: dict[str, Any],
            *,
            current: bool,
        ) -> None:
            item = {
                "source_stream": source_stream,
                "source_id": source_id,
                "relation": relation,
            }
            if current:
                current_backlinks.setdefault(target, {})[(source_stream, source_id, relation)] = item
            else:
                historical_backlinks.setdefault(target, []).append(
                    {
                        **item,
                        "source_revision": record["revision"],
                        "status": record["payload"].get("status"),
                        "recorded_at": record["recorded_at"],
                    }
                )

        for stream in _BACKLINK_STREAMS:
            for record in records.get(stream, []):
                latest_record = latest.get(stream, {}).get(record["id"])
                is_latest = latest_record is not None and latest_record["revision"] == record["revision"]
                is_current = is_latest and _is_current(record)
                for target, relation in _edges(record, stream):
                    if is_current:
                        add(target, stream, record["id"], relation, record, current=True)
                    add(target, stream, record["id"], relation, record, current=False)

        current = {
            target: sorted(values.values(), key=lambda item: (item["source_stream"], item["source_id"], item["relation"]))
            for target, values in current_backlinks.items()
        }
        historical = {
            target: sorted(
                values,
                key=lambda item: (
                    item["source_stream"],
                    item["source_id"],
                    item["source_revision"],
                    item["relation"],
                ),
            )
            for target, values in historical_backlinks.items()
        }
        result = {
            "memory_commit": source_commit,
            "derivation_source_commit": source_commit,
            "profile": f"{repository.profile.name}@{repository.profile.version}",
            "backlinks": dict(sorted(current.items())),
            "current_backlinks": dict(sorted(current.items())),
            "historical_backlinks": dict(sorted(historical.items())),
        }
    if output_path is not None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {"ok": True, **result, "output_path": str(output_path) if output_path else None}
