from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository

from .backlinks import build as build_backlinks
from .paths import bundled_profile


TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]")
CURRENT_ONLY = {"events", "psychologies", "philosophies", "archetypes", "hypotheses"}


def tokenize(text: str) -> list[str]:
    base = [token.lower() for token in TOKEN_RE.findall(text)]
    cjk = [token for token in base if len(token) == 1 and "\u3400" <= token <= "\u9fff"]
    bigrams = [cjk[index] + cjk[index + 1] for index in range(len(cjk) - 1)]
    return base + bigrams


def _vector(tokens: Iterable[str], dimensions: int = 256) -> dict[int, float]:
    counts: Counter[int] = Counter()
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest, "big") % dimensions
        counts[index] += 1
    norm = math.sqrt(sum(value * value for value in counts.values())) or 1.0
    return {index: value / norm for index, value in counts.items()}


def _cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(index, 0.0) for index, value in left.items())


def _text(record: dict[str, Any]) -> str:
    payload = record["payload"]
    keys = ("description", "name", "statement", "change_summary", "content")
    parts = [str(payload[key]) for key in keys if payload.get(key)]
    for key in ("aliases", "evidence_refs", "counter_evidence_refs"):
        parts.extend(str(value) for value in payload.get(key, []))
    sections = payload.get("sections", {})
    for values in sections.values():
        parts.extend(str(value) for value in values)
    for key in ("current_topics", "open_questions", "active_tensions", "recent_decisions", "next_possible_directions"):
        parts.extend(str(value) for value in payload.get(key, []))
    return "\n".join(parts)


def _created(record: dict[str, Any]) -> str:
    payload = record["payload"]
    occurred = payload.get("occurred_at", {})
    return occurred.get("start") or payload.get("created_at") or record["recorded_at"]


def _split_message_text(text: str, token_budget: int) -> list[str]:
    paragraphs = text.splitlines() or [text]
    fragments: list[str] = []
    current: list[str] = []
    current_cost = 0
    for paragraph in paragraphs:
        cost = max(1, len(tokenize(paragraph)))
        if cost > token_budget:
            if current:
                fragments.append("\n".join(current))
                current, current_cost = [], 0
            start = 0
            while start < len(paragraph):
                low, high = 1, len(paragraph) - start
                while low < high:
                    middle = (low + high + 1) // 2
                    if len(tokenize(paragraph[start : start + middle])) <= token_budget:
                        low = middle
                    else:
                        high = middle - 1
                width = max(1, low)
                fragments.append(paragraph[start : start + width])
                start += width
            continue
        if current and current_cost + cost > token_budget:
            fragments.append("\n".join(current))
            current, current_cost = [], 0
        current.append(paragraph)
        current_cost += cost
    if current:
        fragments.append("\n".join(current))
    return fragments or [""]


def _turn_units(turns: list[list[dict[str, Any]]], token_budget: int) -> list[list[tuple[dict[str, Any], str]]]:
    units: list[list[tuple[dict[str, Any], str]]] = []
    for turn in turns:
        unit: list[tuple[dict[str, Any], str]] = []
        cost = 0
        for message in turn:
            for fragment in _split_message_text(message["payload"]["content"], token_budget):
                fragment_cost = max(1, len(tokenize(fragment)))
                if unit and cost + fragment_cost > token_budget:
                    units.append(unit)
                    unit, cost = [], 0
                unit.append((message, fragment))
                cost += fragment_cost
        if unit:
            units.append(unit)
    return units


def _chunks(
    messages: list[dict[str, Any]],
    turns_per_chunk: int = 2,
    overlap: int = 1,
    token_budget: int = 800,
) -> list[dict[str, Any]]:
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        if message["payload"]["kind"] != "conversation":
            continue
        if message["payload"]["role"] == "user" and current:
            turns.append(current)
            current = []
        current.append(message)
    if current:
        turns.append(current)
    units = _turn_units(turns, token_budget)
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(units):
        selected: list[list[tuple[dict[str, Any], str]]] = []
        selected_cost = 0
        for unit in units[start : start + turns_per_chunk]:
            unit_cost = sum(max(1, len(tokenize(fragment))) for _message, fragment in unit)
            if selected and selected_cost + unit_cost > token_budget:
                break
            selected.append(unit)
            selected_cost += unit_cost
        if not selected:
            break
        flattened = [pair for unit in selected for pair in unit]
        messages_in_chunk = [message for message, _fragment in flattened]
        unique_messages = list(dict.fromkeys(message["id"] for message in messages_in_chunk))
        text = "\n".join(f"{message['payload']['role']}: {fragment}" for message, fragment in flattened)
        digest = hashlib.sha256(
            ("|".join(unique_messages) + f"|unit:{start}|" + text + "|chunker@2").encode()
        ).hexdigest()[:20]
        chunks.append(
            {
                "stream": "conversation_chunks",
                "id": f"chunk_{digest}",
                "revision": 1,
                "text": text,
                "recorded_at": messages_in_chunk[-1]["recorded_at"],
                "occurred_at": messages_in_chunk[0]["payload"]["created_at"],
                "evidence_refs": list(
                    dict.fromkeys(
                        f"message:{item['id']}"
                        for item in messages_in_chunk
                        if item["payload"]["role"] == "user"
                    )
                ),
                "links": {"message_ids": unique_messages},
                "current": True,
                "assistant_context": any(item["payload"]["role"] == "assistant" for item in messages_in_chunk),
                "user_evidence_eligible": any(item["payload"]["role"] == "user" for item in messages_in_chunk),
                "prev_id": None,
                "next_id": None,
            }
        )
        if start + len(selected) >= len(units):
            break
        start += max(1, len(selected) - overlap)
    for index, chunk in enumerate(chunks):
        chunk["prev_id"] = chunks[index - 1]["id"] if index else None
        chunk["next_id"] = chunks[index + 1]["id"] if index + 1 < len(chunks) else None
    return chunks


def _documents(repository: MemoryRepository) -> list[dict[str, Any]]:
    records = repository.records_by_stream()
    current_ids = {
        stream: {record_id: item["revision"] for record_id, item in repository.current_records(stream).items()}
        for stream in records
    }
    docs: list[dict[str, Any]] = []
    for stream, items in records.items():
        if stream in {"messages", "checkpoints", "sources"}:
            continue
        for record in items:
            payload = record["payload"]
            current = current_ids[stream].get(record["id"]) == record["revision"]
            docs.append(
                {
                    "stream": stream,
                    "id": record["id"],
                    "revision": record["revision"],
                    "text": _text(record),
                    "recorded_at": record["recorded_at"],
                    "occurred_at": _created(record),
                    "evidence_refs": payload.get("evidence_refs", []),
                    "links": payload.get("links", {}),
                    "status": payload.get("status"),
                    "policy_version": payload.get("policy_version"),
                    "revision_reason": payload.get("revision_reason") or payload.get("change_summary"),
                    "previous_revision": payload.get("previous_revision"),
                    "current": current,
                    "assistant_context": False,
                    "user_evidence_eligible": bool(payload.get("evidence_refs")),
                }
            )
    retrieval_config = repository.profile.raw.get("retrieval", {})
    docs.extend(
        _chunks(
            records.get("messages", []),
            turns_per_chunk=int(retrieval_config.get("chunk_turns", 2)),
            overlap=int(retrieval_config.get("overlap_turns", 1)),
            token_budget=int(retrieval_config.get("chunk_token_budget", 800)),
        )
    )
    return docs


def _build_tantivy(path: Path, docs: list[dict[str, Any]]) -> None:
    import tantivy

    path.mkdir(parents=True, exist_ok=True)
    schema_builder = tantivy.SchemaBuilder()
    schema_builder.add_text_field("key", stored=True, tokenizer_name="raw")
    schema_builder.add_text_field("text", stored=True)
    schema_builder.add_text_field("stream", stored=True, tokenizer_name="raw")
    schema = schema_builder.build()
    index = tantivy.Index(schema, path=str(path))
    writer = index.writer()
    for doc in docs:
        key = f"{doc['stream']}:{doc['id']}@{doc['revision']}"
        writer.add_document(
            tantivy.Document(
                key=key,
                text=" ".join(tokenize(doc["text"])),
                stream=doc["stream"],
            )
        )
    writer.commit()
    index.reload()


def _build_milvus(path: Path, docs: list[dict[str, Any]]) -> None:
    os.environ["NO_PROXY"] = _merge_no_proxy(os.environ.get("NO_PROXY"))
    os.environ["no_proxy"] = _merge_no_proxy(os.environ.get("no_proxy"))
    from pymilvus import MilvusClient

    client = MilvusClient(str(path))
    try:
        client.create_collection(
            "memory",
            dimension=256,
            primary_field_name="key",
            id_type="string",
            max_length=512,
            vector_field_name="vector",
            metric_type="COSINE",
        )
        if docs:
            data = []
            for doc in docs:
                vector = _vector(tokenize(doc["text"]))
                data.append(
                    {
                        "key": f"{doc['stream']}:{doc['id']}@{doc['revision']}",
                        "vector": [float(vector.get(index, 0.0)) for index in range(256)],
                    }
                )
            client.insert("memory", data)
    finally:
        client.close()


def _merge_no_proxy(value: str | None) -> str:
    entries = [item.strip() for item in (value or "").split(",") if item.strip()]
    for item in ("127.0.0.1", "localhost"):
        if item not in entries:
            entries.append(item)
    return ",".join(entries)


def build_index(*, repo_root: Path, indexes_root: str | Path, force: bool = False, **_: Any) -> dict[str, Any]:
    repo_root = Path(repo_root)
    profile_path = repo_root / "profiles" / "pco"
    profile = Profile.load(profile_path if profile_path.exists() else bundled_profile(), default_registry())
    repository = MemoryRepository(repo_root, profile)
    commit = repository.head()
    generation = Path(indexes_root) / "generations" / commit
    manifest_path = generation / "manifest.json"
    if manifest_path.exists() and not force:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return {"ok": True, "idempotent": True, **manifest, "generation_path": str(generation)}
    if generation.exists():
        shutil.rmtree(generation)
    generation.mkdir(parents=True, exist_ok=True)
    docs = _documents(repository)
    terms: dict[str, list[str]] = defaultdict(list)
    vectors: dict[str, dict[str, float]] = {}
    for doc in docs:
        key = f"{doc['stream']}:{doc['id']}@{doc['revision']}"
        tokens = tokenize(doc["text"])
        for term in sorted(set(tokens)):
            terms[term].append(key)
        vectors[key] = {str(index): value for index, value in _vector(tokens).items()}
    (generation / "documents.json").write_text(json.dumps(docs, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    (generation / "lexical.json").write_text(json.dumps(terms, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    (generation / "dense.json").write_text(json.dumps(vectors, sort_keys=True), encoding="utf-8")
    backlink_result = build_backlinks(repo_root=repo_root, output_path=generation / "backlinks.json")
    lexical_backend = "tantivy"
    dense_backend = "milvus-lite"
    backend_errors: dict[str, str] = {}
    try:
        _build_tantivy(generation / "tantivy", docs)
    except Exception as exc:
        lexical_backend = "local-inverted-index"
        backend_errors["tantivy"] = str(exc)
    try:
        _build_milvus(generation / "milvus.db", docs)
    except Exception as exc:
        dense_backend = "local-hashed-vector"
        backend_errors["milvus-lite"] = str(exc)
    manifest = {
        "memory_commit": commit,
        "profile": f"{profile.name}@{profile.version}",
        "documents": len(docs),
        "dense_backend": dense_backend,
        "lexical_backend": lexical_backend,
        "backlinks": len(backlink_result["backlinks"]),
        "backend_errors": backend_errors,
    }
    (generation / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    active = Path(indexes_root) / "active.json"
    active.parent.mkdir(parents=True, exist_ok=True)
    temporary = active.with_suffix(".tmp")
    temporary.write_text(json.dumps({"generation": commit, "manifest": str(generation / "manifest.json")}, sort_keys=True), encoding="utf-8")
    temporary.replace(active)
    return {"ok": True, "idempotent": False, **manifest, "generation_path": str(generation)}


def _eligible_backend_hits(
    search_once: Callable[[int], dict[str, float]],
    *,
    eligible_keys: set[str],
    candidate_limit: int,
    total_documents: int,
) -> dict[str, float]:
    if not eligible_keys or total_documents <= 0:
        return {}
    target = min(candidate_limit, len(eligible_keys))
    fetch_limit = min(total_documents, max(1, candidate_limit))
    while True:
        raw = search_once(fetch_limit)
        eligible = {key: score for key, score in raw.items() if key in eligible_keys}
        if len(eligible) >= target or fetch_limit >= total_documents or len(raw) < fetch_limit:
            return dict(list(eligible.items())[:target])
        expanded = min(total_documents, max(fetch_limit + 1, fetch_limit * 2))
        if expanded == fetch_limit:
            return dict(list(eligible.items())[:target])
        fetch_limit = expanded


def _index_scores(
    *,
    generation: Path,
    manifest: dict[str, Any],
    query: str,
    eligible_keys: set[str],
    candidate_limit: int,
) -> tuple[dict[str, float], dict[str, float]]:
    dense: dict[str, float] = {}
    lexical: dict[str, float] = {}
    total_documents = int(manifest.get("documents", 0))
    if manifest.get("lexical_backend") == "tantivy" and query.strip():
        try:
            import tantivy

            index = tantivy.Index.open(str(generation / "tantivy"))
            terms = tokenize(query)
            parsed = index.parse_query(" OR ".join(terms), ["text"])
            searcher = index.searcher()

            def lexical_search(fetch_limit: int) -> dict[str, float]:
                hits: dict[str, float] = {}
                for score, address in searcher.search(parsed, limit=fetch_limit).hits:
                    key = searcher.doc(address).get_first("key")
                    hits[str(key)] = float(score)
                return hits

            lexical = _eligible_backend_hits(
                lexical_search,
                eligible_keys=eligible_keys,
                candidate_limit=candidate_limit,
                total_documents=total_documents,
            )
        except Exception:
            lexical = {}
    if manifest.get("dense_backend") == "milvus-lite" and eligible_keys:
        try:
            os.environ["NO_PROXY"] = _merge_no_proxy(os.environ.get("NO_PROXY"))
            os.environ["no_proxy"] = _merge_no_proxy(os.environ.get("no_proxy"))
            from pymilvus import MilvusClient

            client = MilvusClient(str(generation / "milvus.db"))
            try:
                vector = _vector(tokenize(query))

                def dense_search(fetch_limit: int) -> dict[str, float]:
                    response = client.search(
                        "memory",
                        data=[[float(vector.get(index, 0.0)) for index in range(256)]],
                        limit=fetch_limit,
                        output_fields=["key"],
                    )
                    hits: dict[str, float] = {}
                    for hit in response[0]:
                        key = hit.get("entity", {}).get("key") or hit.get("key") or hit.get("id")
                        hits[str(key)] = float(hit.get("distance", 0.0))
                    return hits

                dense = _eligible_backend_hits(
                    dense_search,
                    eligible_keys=eligible_keys,
                    candidate_limit=candidate_limit,
                    total_documents=total_documents,
                )
            finally:
                client.close()
        except Exception:
            dense = {}
    return dense, lexical


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        try:
            return datetime.fromisoformat(value[:10]).replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _linked_ids(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_linked_ids(item))
        return result
    if isinstance(value, dict):
        result = set()
        for item in value.values():
            result.update(_linked_ids(item))
        return result
    return set()


def _graph_neighbors(doc: dict[str, Any], backlinks: dict[str, list[dict[str, Any]]]) -> set[str]:
    result = _linked_ids(doc.get("links", {}))
    result.update(doc.get("evidence_refs", []))
    for key in (doc["id"], *doc.get("evidence_refs", [])):
        for item in backlinks.get(key, []):
            result.add(item["source_id"])
    return result


def _matches_neighbor(doc: dict[str, Any], neighbor: str) -> bool:
    if doc["id"] == neighbor:
        return True
    if neighbor.startswith("message:"):
        return neighbor.split(":", 1)[1] in doc.get("links", {}).get("message_ids", [])
    return neighbor in doc.get("links", {}).get("message_ids", [])


def _change_window_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    timed = [
        (when, item)
        for item in items
        if (when := _parse_time(item.get("occurred_at") or item.get("recorded_at"))) is not None
    ]
    timed.sort(key=lambda pair: pair[0])
    if not timed:
        return {"split_at": None, "earlier": {}, "later": {}, "caution": "No comparable timestamps are available."}
    split = timed[len(timed) // 2][0]
    earlier: Counter[str] = Counter()
    later: Counter[str] = Counter()
    for when, item in timed:
        (earlier if when <= split else later)[item["stream"]] += 1
    return {
        "split_at": split.isoformat(),
        "earlier": dict(earlier),
        "later": dict(later),
        "caution": "Missing records are not evidence that a pattern or event did not exist.",
    }


def search(
    *,
    repo_root: Path,
    query: str,
    mode: str = "current",
    limit: int = 10,
    start: str | None = None,
    end: str | None = None,
    indexes_root: str | Path | None = None,
    **_: Any,
) -> dict[str, Any]:
    if mode not in {"continuity", "current", "pattern", "historical", "change"}:
        raise ValueError(f"Unknown retrieval mode: {mode}")
    repo_root = Path(repo_root)
    profile_path = repo_root / "profiles" / "pco"
    profile = Profile.load(profile_path if profile_path.exists() else bundled_profile(), default_registry())
    repository = MemoryRepository(repo_root, profile)
    docs = _documents(repository)
    retrieval_config = profile.raw.get("retrieval", {})
    candidate_limit = max(limit, int(retrieval_config.get("candidate_count", 200)))
    rrf_k = float(retrieval_config.get("rrf_k", 60))
    recency_half_life_days = max(0.000001, float(retrieval_config.get("recency_half_life_days", 180)))
    indexes_root = Path(indexes_root) if indexes_root is not None else repo_root.parent / "indexes"
    index_result = build_index(repo_root=repo_root, indexes_root=indexes_root)
    generation = Path(index_result["generation_path"])
    start_at, end_at = _parse_time(start), _parse_time(end)
    filtered: list[dict[str, Any]] = []
    for doc in docs:
        when = _parse_time(doc.get("occurred_at") or doc.get("recorded_at"))
        if start_at and when and when < start_at:
            continue
        if end_at and when and when > end_at:
            continue
        if mode in {"current", "continuity", "pattern"} and doc["stream"] in CURRENT_ONLY and not doc["current"]:
            continue
        if mode in {"current", "continuity"} and doc["stream"] in {"meta_revisions", "continuations"} and not doc["current"]:
            continue
        if mode == "current" and doc.get("status") in {"disputed", "rejected", "superseded", "tombstone"}:
            continue
        if mode == "continuity" and doc["stream"] not in {"continuations", "conversation_chunks"}:
            continue
        if mode == "pattern" and doc["stream"] not in {"events", "hypotheses", "conversation_chunks", "psychologies", "philosophies", "archetypes"}:
            continue
        if mode == "change" and doc["stream"] not in {"events", "hypotheses", "meta_revisions", "conversation_chunks"}:
            continue
        filtered.append(doc)

    eligible_keys = {
        f"{doc['stream']}:{doc['id']}@{doc['revision']}"
        for doc in filtered
    }
    backend_dense, backend_lexical = _index_scores(
        generation=generation,
        manifest=index_result,
        query=query,
        eligible_keys=eligible_keys,
        candidate_limit=candidate_limit,
    )
    backend_candidates = set(backend_dense) | set(backend_lexical)
    if backend_candidates:
        backend_filtered = [
            doc
            for doc in filtered
            if f"{doc['stream']}:{doc['id']}@{doc['revision']}" in backend_candidates
        ]
        if backend_filtered:
            filtered = backend_filtered
        else:
            backend_candidates = set()
    if not backend_candidates and len(filtered) > candidate_limit:
        # Replaceable local indexes may be unavailable. Keep a bounded,
        # deterministic recent pool so Python reranking never expands to the
        # entire canonical corpus.
        filtered = sorted(
            filtered,
            key=lambda doc: doc.get("occurred_at") or doc.get("recorded_at") or "",
            reverse=True,
        )[:candidate_limit]

    query_tokens = tokenize(query)
    query_counts = Counter(query_tokens)
    query_vector = _vector(query_tokens)
    dense = {}
    for index, doc in enumerate(filtered):
        key = f"{doc['stream']}:{doc['id']}@{doc['revision']}"
        dense[index] = backend_dense.get(key, _cosine(query_vector, _vector(tokenize(doc["text"]))))
    lexical: dict[int, float] = {}
    for index, doc in enumerate(filtered):
        counts = Counter(tokenize(doc["text"]))
        fallback = sum(min(counts[token], count) for token, count in query_counts.items()) / (len(query_tokens) or 1)
        key = f"{doc['stream']}:{doc['id']}@{doc['revision']}"
        lexical[index] = backend_lexical.get(key, fallback)
    dense_rank = {index: rank for rank, (index, _) in enumerate(sorted(dense.items(), key=lambda item: item[1], reverse=True), 1)}
    lexical_rank = {index: rank for rank, (index, _) in enumerate(sorted(lexical.items(), key=lambda item: item[1], reverse=True), 1)}
    now = datetime.now(timezone.utc)
    scored: list[dict[str, Any]] = []
    for index, doc in enumerate(filtered):
        when = _parse_time(doc.get("occurred_at") or doc.get("recorded_at"))
        age_days = max(0.0, (now - when).total_seconds() / 86400) if when else 3650.0
        time_score = math.exp(-math.log(2) * age_days / recency_half_life_days)
        rrf = 1 / (rrf_k + dense_rank[index]) + 1 / (rrf_k + lexical_rank[index])
        if mode == "continuity":
            rrf += time_score * 0.02
        if mode == "pattern" and doc["stream"] in {"events", "hypotheses"}:
            rrf += 0.005
        item = dict(doc)
        item.update(
            {
                "dense_score": round(dense[index], 8),
                "lexical_score": round(lexical[index], 8),
                "rrf_score": round(rrf, 8),
                "time_score": round(time_score, 8),
                "graph_score": 0.0,
                "retrieval_mode": mode,
            }
        )
        scored.append(item)
    if mode in {"pattern", "historical", "change"} and scored:
        backlink_map = build_backlinks(repo_root=repo_root)["backlinks"]
        seeds = sorted(scored, key=lambda item: (item["rrf_score"], item["time_score"]), reverse=True)[:5]
        neighbors: set[str] = set()
        for seed in seeds:
            neighbors.update(_graph_neighbors(seed, backlink_map))
        for item in scored:
            matches = sum(1 for neighbor in neighbors if _matches_neighbor(item, neighbor))
            if matches:
                item["graph_score"] = round(min(0.02, matches * 0.004), 8)
                item["rrf_score"] = round(item["rrf_score"] + item["graph_score"], 8)
    scored.sort(key=lambda item: (item["rrf_score"], item["time_score"]), reverse=True)
    response = {
        "ok": True,
        "query": query,
        "mode": mode,
        "memory_commit": repository.head(),
        "backends": {"dense": index_result["dense_backend"], "lexical": index_result["lexical_backend"]},
        "retrieval_policy": {
            "candidate_count": candidate_limit,
            "rrf_k": rrf_k,
            "recency_half_life_days": recency_half_life_days,
        },
        "results": scored[:limit],
    }
    if mode == "change":
        response["change_windows"] = _change_window_summary(scored)
    return response
