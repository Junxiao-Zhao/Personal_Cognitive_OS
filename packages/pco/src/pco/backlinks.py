"""Pure internal reverse-reference construction for retrieval generations."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def build_reverse_references(
    documents: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Return deterministic target-to-source edges without mutating documents."""

    edges: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        source_stream = document.get("stream")
        source_id = document.get("id")
        revision = document.get("revision")
        if not isinstance(source_stream, str) or not isinstance(source_id, str):
            continue
        if not isinstance(revision, int) or isinstance(revision, bool):
            continue
        relations: list[tuple[str, str]] = []
        for field in ("evidence_refs", "counter_evidence_refs", "promotion_refs"):
            values = document.get(field, [])
            if isinstance(values, list):
                relations.extend(
                    (value, field) for value in values if isinstance(value, str)
                )
        links = document.get("links", {})
        if isinstance(links, Mapping):
            for relation, values in links.items():
                if isinstance(relation, str) and isinstance(values, list):
                    relations.extend(
                        (value, relation) for value in values if isinstance(value, str)
                    )
        for target, relation in relations:
            edges.setdefault(target, []).append(
                {
                    "source_id": source_id,
                    "source_revision": revision,
                    "source_stream": source_stream,
                    "relation": relation,
                }
            )
    return {
        target: sorted(
            values,
            key=lambda item: (
                item["source_stream"].encode("utf-8"),
                item["source_id"].encode("utf-8"),
                item["source_revision"],
                item["relation"].encode("utf-8"),
            ),
        )
        for target, values in sorted(
            edges.items(), key=lambda item: item[0].encode("utf-8")
        )
    }
