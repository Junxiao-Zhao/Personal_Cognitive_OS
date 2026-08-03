from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository

from .paths import bundled_profile


SECTION_LABELS = {
    "deep_impressions": "当前深层印象",
    "stable_preferences_and_values": "稳定偏好与价值",
    "active_patterns": "活跃模式",
    "important_tensions": "重要矛盾",
    "recent_changes": "近期变化",
    "open_questions": "开放问题",
    "boundaries": "认识边界",
}


def _current(repository: MemoryRepository, stream: str, record_id: str) -> dict[str, Any] | None:
    return repository.current_records(stream).get(record_id)


def render(*, repo_root: Path, output_path: str | Path, checkpoint_id: str | None = None, **_: Any) -> dict[str, Any]:
    repo_root = Path(repo_root)
    profile_path = repo_root / "profiles" / "pco"
    profile = Profile.load(profile_path if profile_path.exists() else bundled_profile(), default_registry())
    repository = MemoryRepository(repo_root, profile)
    meta = _current(repository, "meta_revisions", "meta_current")
    continuation = _current(repository, "continuations", "continuation_current")
    lines = [
        "# PCO 当前上下文",
        "",
        "> 这是从 canonical memory 确定性生成的当前快照。只把用户消息、注册来源和事件视为用户证据；assistant 文本仅作交互上下文。",
        "",
    ]
    if meta:
        lines.extend([f"## Meta-memory · revision {meta['revision']}", ""])
        for key, label in SECTION_LABELS.items():
            values = meta["payload"]["sections"].get(key, [])
            lines.extend([f"### {label}", ""])
            lines.extend([f"- {value}" for value in values] or ["- 暂无已批准认识"])
            lines.append("")
    else:
        lines.extend(["## Meta-memory", "", "尚无已批准的 Meta-memory。不要据此假定已了解用户。", ""])
    if continuation:
        payload = continuation["payload"]
        lines.extend([f"## Continuation · revision {continuation['revision']}", ""])
        continuation_labels = {
            "current_topics": "当前话题",
            "open_questions": "尚未回答的问题",
            "active_tensions": "正在探索的矛盾",
            "recent_decisions": "近期决定",
            "next_possible_directions": "自然的下一步",
        }
        for key, label in continuation_labels.items():
            lines.append(f"### {label}")
            lines.append("")
            lines.extend([f"- {value}" for value in payload.get(key, [])] or ["- 无"])
            lines.append("")
    else:
        lines.extend(["## Continuation", "", "尚无 continuation；从当前用户输入自然开始。", ""])
    content = "\n".join(lines).rstrip() + "\n"
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "ok": True,
        "checkpoint_id": checkpoint_id,
        "memory_commit": repository.head(),
        "meta_revision": meta["revision"] if meta else None,
        "continuation_revision": continuation["revision"] if continuation else None,
        "rendered_context_path": str(target),
        "content_hash": digest,
    }
