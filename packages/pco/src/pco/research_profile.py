from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def validate_profile(_repo_root: Path, records: dict[str, list[dict[str, Any]]]) -> Iterable[dict[str, Any]]:
    observation_ids = {record["id"] for record in records.get("observations", [])}
    for claim in records.get("claims", []):
        for index, reference in enumerate(claim["payload"]["observation_refs"]):
            if reference not in observation_ids:
                yield {
                    "code": "REFERENCE_NOT_FOUND", "phase": "profile_validation", "message": f"Unknown observation: {reference}",
                    "stream": "claims", "record_id": claim["id"], "path": f"/payload/observation_refs/{index}", "value": reference,
                    "retryable": True, "recovery": ["Append the observation first", "Remove the reference"]
                }


def search(*, repo_root: Path, query: str, **_: Any) -> dict[str, Any]:
    query_lower = query.lower()
    results: list[dict[str, Any]] = []
    for stream, relative in (("observations", "research/observations.jsonl"), ("claims", "research/claims.jsonl")):
        path = Path(repo_root) / relative
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            text = json.dumps(record["payload"], ensure_ascii=False)
            if query_lower in text.lower():
                results.append({"stream": stream, "id": record["id"], "revision": record["revision"], "text": text})
    return {"ok": True, "results": results}


def project_markdown(*, repo_root: Path, output_root: str | Path, **_: Any) -> dict[str, Any]:
    target = Path(output_root)
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for source in ("research/observations.jsonl", "research/claims.jsonl"):
        for line in (Path(repo_root) / source).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            (target / f"{record['id']}.md").write_text(f"# {record['id']}\n\n```json\n{json.dumps(record, ensure_ascii=False, indent=2)}\n```\n", encoding="utf-8")
            count += 1
    return {"ok": True, "pages": count, "output_root": str(target)}
