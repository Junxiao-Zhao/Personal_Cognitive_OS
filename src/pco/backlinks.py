from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository

from .paths import bundled_profile


def _profile(repo_root: Path) -> Profile:
    canonical = repo_root / "profiles" / "pco"
    return Profile.load(canonical if canonical.exists() else bundled_profile(), default_registry())


def build(*, repo_root: Path, output_path: str | Path | None = None, **_: Any) -> dict[str, Any]:
    repo_root = Path(repo_root)
    repository = MemoryRepository(repo_root, _profile(repo_root))
    records = repository.records_by_stream()
    backlinks: dict[str, list[dict[str, Any]]] = {}

    def add(target: str, source_stream: str, source_id: str, relation: str) -> None:
        backlinks.setdefault(target, []).append(
            {"source_stream": source_stream, "source_id": source_id, "relation": relation}
        )

    for event in records.get("events", []):
        for target_stream, ids in event["payload"].get("links", {}).items():
            for target_id in ids:
                add(target_id, "events", event["id"], target_stream)
    for stream in ("events", "archetypes", "hypotheses", "meta_revisions"):
        for record in records.get(stream, []):
            for field in ("evidence_refs", "counter_evidence_refs"):
                for ref in record["payload"].get(field, []):
                    add(ref, stream, record["id"], field)
    for meta in records.get("meta_revisions", []):
        for target_id in meta["payload"].get("promotion_refs", []):
            add(target_id, "meta_revisions", meta["id"], "promotion")

    for values in backlinks.values():
        values.sort(key=lambda item: (item["source_stream"], item["source_id"], item["relation"]))
    result = {
        "memory_commit": repository.head(),
        "profile": f"{repository.profile.name}@{repository.profile.version}",
        "backlinks": dict(sorted(backlinks.items())),
    }
    if output_path is not None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {"ok": True, **result, "output_path": str(output_path) if output_path else None}
