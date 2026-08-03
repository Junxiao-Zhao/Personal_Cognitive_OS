from __future__ import annotations

import json
import os
import shlex
import subprocess
import re
from pathlib import Path
from typing import Any

from mem_core.errors import MemError
from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository

from .backlinks import build as build_backlinks
from .paths import bundled_profile


STREAM_TITLES = {
    "events": "事件",
    "psychologies": "心理概念",
    "philosophies": "哲学概念",
    "archetypes": "人物投影与原型",
    "hypotheses": "待验证认识",
    "meta_revisions": "认识变化",
    "checkpoints": "Checkpoint 更新记录",
}


def _repository(repo_root: Path) -> MemoryRepository:
    profile_path = repo_root / "profiles" / "pco"
    profile = Profile.load(profile_path if profile_path.exists() else bundled_profile(), default_registry())
    return MemoryRepository(repo_root, profile)


def _load_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"entities": {}, "commits": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _record_title(stream: str, record: dict[str, Any]) -> str:
    payload = record["payload"]
    return payload.get("name") or payload.get("description") or payload.get("statement") or record["id"]


def _page(stream: str, record_id: str, history: list[dict[str, Any]], backlinks: dict[str, Any]) -> dict[str, Any]:
    latest = history[-1]
    title = _record_title(stream, latest)
    lines = [f"# {title}", "", f"PCO entity ID: `{record_id}`", f"Stream: `{stream}`", ""]
    for record in history:
        lines.extend([f"## Revision {record['revision']} · {record['recorded_at']}", ""])
        payload = record["payload"]
        for key, value in payload.items():
            if key == "sections":
                for section, entries in value.items():
                    lines.extend([f"### {section}", ""])
                    lines.extend([f"- {entry}" for entry in entries] or ["- 无"])
                    lines.append("")
            elif isinstance(value, list):
                lines.extend([f"### {key}", ""])
                lines.extend([f"- {json.dumps(entry, ensure_ascii=False) if isinstance(entry, dict) else entry}" for entry in value] or ["- 无"])
                lines.append("")
            elif isinstance(value, dict):
                lines.extend([f"### {key}", "", "```json", json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), "```", ""])
            else:
                lines.extend([f"- **{key}**: {value}", ""])
    related = backlinks.get(record_id, [])
    if related:
        lines.extend(["## Backlinks", ""])
        lines.extend([f"- `{item['source_stream']}:{item['source_id']}` · {item['relation']}" for item in related])
        lines.append("")
    return {"entity_id": record_id, "stream": stream, "title": title, "content": "\n".join(lines).rstrip() + "\n"}


def _pages(repository: MemoryRepository) -> list[dict[str, Any]]:
    records = repository.records_by_stream()
    backlink_map = build_backlinks(repo_root=repository.root)["backlinks"]
    pages: list[dict[str, Any]] = []
    indexes: dict[str, list[tuple[str, str]]] = {}
    for stream in STREAM_TITLES:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records.get(stream, []):
            grouped.setdefault(record["id"], []).append(record)
        indexes[stream] = []
        for record_id, history in grouped.items():
            page = _page(stream, record_id, history, backlink_map)
            pages.append(page)
            indexes[stream].append((page["title"], record_id))
    for stream, label in STREAM_TITLES.items():
        lines = [f"# {label}索引", "", f"Canonical commit: `{repository.head()}`", ""]
        lines.extend(
            [f"- [{title}](pco://{entity_id})" for title, entity_id in sorted(indexes[stream])]
            or ["- 暂无记录"]
        )
        pages.append(
            {
                "entity_id": f"index_{stream}",
                "stream": "indexes",
                "title": f"{label}索引",
                "content": "\n".join(lines).rstrip() + "\n",
            }
        )
    meta = repository.current_records("meta_revisions").get("meta_current")
    home = ["# PCO 首页", "", f"Canonical commit: `{repository.head()}`", ""]
    if meta:
        home.extend([f"## 当前人格侧写 · revision {meta['revision']}", "", meta["payload"]["change_summary"], ""])
        for section, values in meta["payload"]["sections"].items():
            home.extend([f"### {section}", ""])
            home.extend([f"- {value}" for value in values] or ["- 无"])
            home.append("")
    else:
        home.extend(["## 当前人格侧写", "", "尚无已批准的 Meta-memory。", ""])
    for stream, label in STREAM_TITLES.items():
        home.extend([f"## [{label}](pco://index_{stream})", ""])
        home.extend([f"- [{title}](pco://{entity_id})" for title, entity_id in sorted(indexes[stream])] or ["- 暂无记录"])
        home.append("")
    pages.insert(0, {"entity_id": "pco_home", "stream": "home", "title": "PCO 首页", "content": "\n".join(home).rstrip() + "\n"})
    return pages


def project_markdown(*, repo_root: Path, output_root: str | Path, **_: Any) -> dict[str, Any]:
    repository = _repository(Path(repo_root))
    commit = repository.head()
    target_root = Path(output_root)
    mapping_path = target_root / ".pco-projection.json"
    mapping = _load_mapping(mapping_path)
    if mapping.get("commits", {}).get(commit) == "complete":
        return {"ok": True, "idempotent": True, "target": "markdown", "memory_commit": commit, "pages": 0}
    pages = _pages(repository)
    paths = {
        page["entity_id"]: target_root / page["stream"] / f"{page['entity_id']}.md"
        for page in pages
    }
    for page in pages:
        directory = target_root / page["stream"]
        directory.mkdir(parents=True, exist_ok=True)
        path = paths[page["entity_id"]]
        content = re.sub(
            r"pco://([A-Za-z0-9_.-]+)",
            lambda match: Path(os.path.relpath(paths[match.group(1)], start=path.parent)).as_posix()
            if match.group(1) in paths
            else match.group(0),
            page["content"],
        )
        path.write_text(content, encoding="utf-8")
        mapping["entities"][page["entity_id"]] = {"target_id": str(path), "stream": page["stream"], "last_commit": commit}
    mapping.setdefault("commits", {})[commit] = "complete"
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = mapping_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(mapping_path)
    return {"ok": True, "idempotent": False, "target": "markdown", "memory_commit": commit, "pages": len(pages), "output_root": str(target_root)}


def project_affine(
    *,
    repo_root: Path,
    state_root: str | Path,
    command: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Project through a user-installed AFFiNE bridge CLI.

    AFFiNE has no stable public content-write API. The bridge contract is one JSON
    request on stdin and one JSON response on stdout, keeping provider-specific
    CRDT/Yjs behavior outside canonical memory and mem-core.
    """
    repository = _repository(Path(repo_root))
    commit = repository.head()
    state = Path(state_root) / "affine"
    mapping_path = state / "mapping.json"
    outbox_path = state / "outbox" / f"{commit}.json"
    mapping = _load_mapping(mapping_path)
    if mapping.get("commits", {}).get(commit) == "complete":
        return {"ok": True, "idempotent": True, "target": "affine", "memory_commit": commit, "pages": 0}
    pages = _pages(repository)
    request = {"operation": "upsert_pages", "memory_commit": commit, "pages": pages, "mapping": mapping.get("entities", {})}
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    outbox_temporary = outbox_path.with_suffix(".tmp")
    outbox_temporary.write_text(json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    outbox_temporary.replace(outbox_path)
    bridge = command or os.getenv("PCO_AFFINE_COMMAND")
    if not bridge:
        return {
            "ok": False,
            "pending": True,
            "target": "affine",
            "memory_commit": commit,
            "pages": len(pages),
            "outbox": str(outbox_path),
            "error": {"code": "AFFINE_BRIDGE_NOT_CONFIGURED", "retryable": True, "recovery": ["Set PCO_AFFINE_COMMAND to an AFFiNE bridge CLI", "Use the Markdown projection target"]},
        }
    try:
        result = subprocess.run(
            shlex.split(bridge),
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            check=False,
            timeout=float(os.getenv("PCO_AFFINE_TIMEOUT_SECONDS", "120")),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "pending": True,
            "target": "affine",
            "memory_commit": commit,
            "outbox": str(outbox_path),
            "error": {"code": "AFFINE_SYNC_TIMEOUT", "retryable": True},
        }
    if result.returncode:
        diagnostic_path = state / "logs" / f"{commit}.log"
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        diagnostic_path.write_text(result.stderr, encoding="utf-8")
        diagnostic_path.chmod(0o600)
        return {
            "ok": False,
            "pending": True,
            "target": "affine",
            "memory_commit": commit,
            "outbox": str(outbox_path),
            "error": {
                "code": "AFFINE_SYNC_FAILED",
                "message": f"AFFiNE bridge exited with code {result.returncode}",
                "diagnostic_path": str(diagnostic_path),
                "retryable": True,
            },
        }
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MemError("AFFINE_BRIDGE_INVALID_RESPONSE", "projection", str(exc), retryable=True) from exc
    response_mapping = response.get("mapping", {})
    expected_ids = {page["entity_id"] for page in pages}
    valid = (
        response.get("ok") is True
        and response.get("memory_commit") == commit
        and isinstance(response_mapping, dict)
        and expected_ids.issubset(response_mapping)
        and all(isinstance(response_mapping[entity_id], str) and response_mapping[entity_id] for entity_id in expected_ids)
    )
    if not valid:
        raise MemError(
            "AFFINE_BRIDGE_INVALID_RESPONSE",
            "projection",
            "Bridge must acknowledge the same memory commit and map every requested entity",
            retryable=True,
        )
    for entity_id, target_id in response_mapping.items():
        existing = mapping["entities"].get(entity_id, {})
        mapping["entities"][entity_id] = {**existing, "target_id": target_id, "last_commit": commit}
    mapping.setdefault("commits", {})[commit] = "complete"
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = mapping_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(mapping_path)
    return {"ok": True, "idempotent": False, "target": "affine", "memory_commit": commit, "pages": len(pages)}
