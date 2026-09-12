"""Disposable retrieval generation construction and read-only search."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Iterable, Iterator, Mapping

from mem_core.errors import MemError
from mem_core.profile import Profile
from mem_core.registry import default_registry
from mem_core.repository import MemoryRepository

from .backlinks import build_reverse_references
from .derivation_manifest import (
    DerivationManifest,
    _read_tree_no_follow,
    canonical_manifest_bytes,
    derivation_error,
    manifest_for_files,
    verify_derivation_tree,
)
from .projections import _write_new_tree, exact_canonical_records
from .paths import bundled_profile
from .validation import latest_effective

TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]")
SEARCH_MODES = frozenset({"current", "historical", "change", "pattern", "continuity"})
CANONICAL_FALLBACK_CANDIDATES = 200
BACKEND_CANDIDATES = 200


def tokenize(text: str) -> list[str]:
    base = [token.lower() for token in TOKEN_RE.findall(text)]
    cjk = [token for token in base if len(token) == 1 and "\u3400" <= token <= "\u9fff"]
    return base + [cjk[index] + cjk[index + 1] for index in range(len(cjk) - 1)]


def _vector(tokens: Iterable[str], dimensions: int = 256) -> list[float]:
    counts: Counter[int] = Counter()
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        counts[int.from_bytes(digest, "big") % dimensions] += 1
    norm = math.sqrt(sum(value * value for value in counts.values())) or 1.0
    return [float(counts.get(index, 0) / norm) for index in range(dimensions)]


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _document_text(record: Mapping[str, Any]) -> str:
    payload = record.get("payload", {})
    if not isinstance(payload, Mapping):
        return ""
    parts: list[str] = []
    for key in (
        "body_markdown",
        "name",
        "title",
        "locator",
        "provider_label",
        "change_summary",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    for key in (
        "used_excerpts",
        "aliases",
        "topics",
        "unanswered_questions",
        "active_tensions",
        "recent_decisions",
        "next_directions",
    ):
        values = payload.get(key)
        if isinstance(values, list):
            parts.extend(value for value in values if isinstance(value, str))
    return "\n".join(parts)


def _occurred_at(record: Mapping[str, Any]) -> str:
    payload = record.get("payload", {})
    if isinstance(payload, Mapping):
        occurred = payload.get("occurred_at")
        if isinstance(occurred, Mapping) and isinstance(occurred.get("start"), str):
            return occurred["start"]
        for key in ("retrieved_at", "created_at"):
            if isinstance(payload.get(key), str):
                return payload[key]
    recorded_at = record.get("recorded_at")
    return recorded_at if isinstance(recorded_at, str) else ""


def canonical_documents(
    repository: MemoryRepository,
    source_commit: str,
    *,
    canonical_head: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Build deterministic retrieval documents from exact Git-object bytes."""

    profile, records = exact_canonical_records(
        repository, source_commit, canonical_head=canonical_head
    )
    effective = {
        stream: {
            record_id: item["revision"]
            for record_id, item in latest_effective(items).items()
        }
        for stream, items in records.items()
    }
    documents: list[dict[str, Any]] = []
    for stream in sorted(records, key=str.encode):
        for record in records[stream]:
            payload = record.get("payload", {})
            if not isinstance(payload, Mapping):
                payload = {}
            revision = record.get("revision")
            record_id = record.get("id")
            documents.append(
                {
                    "stream": stream,
                    "id": record_id,
                    "revision": revision,
                    "text": _document_text(record),
                    "recorded_at": record.get("recorded_at"),
                    "occurred_at": _occurred_at(record),
                    "status": payload.get("status"),
                    "current": effective.get(stream, {}).get(record_id) == revision,
                    "previous_revision": payload.get("previous_revision"),
                    "change_summary": payload.get("change_summary"),
                    "evidence_refs": list(payload.get("evidence_refs", [])),
                    "counter_evidence_refs": list(
                        payload.get("counter_evidence_refs", [])
                    ),
                    "promotion_refs": list(payload.get("promotion_refs", [])),
                    "links": dict(payload.get("links", {})),
                }
            )
    return sorted(documents, key=_document_key), profile.version


def _document_key(document: Mapping[str, Any]) -> tuple[bytes, bytes, int]:
    return (
        str(document.get("stream", "")).encode("utf-8"),
        str(document.get("id", "")).encode("utf-8"),
        int(document.get("revision", 0)),
    )


def build_tantivy(path: Path, documents: list[dict[str, Any]]) -> None:
    import tantivy

    path.mkdir()
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("key", stored=True, tokenizer_name="raw")
    builder.add_text_field("text", stored=True)
    builder.add_text_field("stream", stored=True, tokenizer_name="raw")
    schema = builder.build()
    index = tantivy.Index(schema, path=str(path))
    writer = index.writer()
    for document in documents:
        writer.add_document(
            tantivy.Document(
                key=_key(document),
                text=" ".join(tokenize(document["text"])),
                stream=document["stream"],
            )
        )
    writer.commit()
    del writer
    index.reload()


@contextmanager
def _local_backend_environment() -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in ("NO_PROXY", "no_proxy")}
    try:
        for name, value in previous.items():
            entries = [
                item.strip() for item in (value or "").split(",") if item.strip()
            ]
            for item in ("127.0.0.1", "localhost"):
                if item not in entries:
                    entries.append(item)
            os.environ[name] = ",".join(entries)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def build_milvus(path: Path, documents: list[dict[str, Any]]) -> None:
    with _local_backend_environment():
        from pymilvus import MilvusClient

        client = None
        try:
            client = MilvusClient(str(path), dedicated=True)
            client.create_collection(
                "memory",
                dimension=256,
                primary_field_name="key",
                id_type="string",
                max_length=512,
                vector_field_name="vector",
                metric_type="COSINE",
            )
            if documents:
                client.insert(
                    "memory",
                    [
                        {
                            "key": _key(document),
                            "vector": _vector(tokenize(document["text"])),
                        }
                        for document in documents
                    ],
                )
                client.flush("memory")
        finally:
            _close_milvus(client, path)


def _close_milvus(client: Any, path: Path) -> None:
    """Close both the pymilvus channel and its path-keyed Lite server."""

    try:
        if client is not None:
            client.close()
    finally:
        from milvus_lite import server_manager_instance

        server_manager_instance.release_server(str(path))


def build_retrieval_generation(
    repository: MemoryRepository, target: Path, source_commit: str
) -> DerivationManifest:
    """Build one complete caller-owned generation without publishing it."""

    documents, profile_version = canonical_documents(repository, source_commit)
    target = Path(target).absolute()
    _write_new_tree(
        target,
        {
            "documents.json": _canonical_json(documents),
            "reverse_refs.json": _canonical_json(build_reverse_references(documents)),
        },
    )
    root_fd: int | None = None
    try:
        observed = os.lstat(target)
        if not stat.S_ISDIR(observed.st_mode):
            raise derivation_error(
                "Retrieval target is not a real directory", path=target
            )
        identity = (observed.st_dev, observed.st_ino)
        root_fd = os.open(
            target,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        after_open = os.fstat(root_fd)
        if (after_open.st_dev, after_open.st_ino) != identity:
            raise derivation_error("Retrieval target identity changed", path=target)
        anchored = Path(f"/proc/self/fd/{root_fd}")
        try:
            build_tantivy(anchored / "tantivy", documents)
            build_milvus(anchored / "milvus.db", documents)
        except Exception as exc:
            raise derivation_error(
                "Retrieval backend construction failed", path=target, value=str(exc)
            ) from exc
        current = os.lstat(target)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (
                current.st_dev,
                current.st_ino,
            )
            != identity
        ):
            raise derivation_error("Retrieval target hierarchy changed", path=target)
        payloads, directories = _read_tree_no_follow(target, identity)
        _materialize_empty_backend_directories(root_fd, payloads, directories)
        payloads, _directories = _read_tree_no_follow(target, identity)
        if "manifest.json" in payloads:
            raise derivation_error(
                "Retrieval target unexpectedly contains a manifest", path=target
            )
        manifest = manifest_for_files(
            payloads,
            kind="retrieval_index",
            source_commit=source_commit,
            profile_version=profile_version,
        )
        descriptor = os.open(
            "manifest.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=root_fd,
        )
        try:
            view = memoryview(canonical_manifest_bytes(manifest))
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        final = verify_derivation_tree(
            target, kind="retrieval_index", expected_commit=source_commit
        )
        if not final["ok"]:
            raise derivation_error(
                "Completed retrieval generation failed verification",
                path=target,
                value=final,
            )
        return manifest
    except MemError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise derivation_error(
            "Retrieval generation could not be built safely",
            path=target,
            value=str(exc),
        ) from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _materialize_empty_backend_directories(
    root_fd: int, payloads: Mapping[str, bytes], directories: set[str]
) -> None:
    """Make backend-created empty directories expressible by the file manifest."""

    implied: set[str] = set()
    for relative in payloads:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            implied.add(parent.as_posix())
            parent = parent.parent
    for relative in sorted(directories - implied, key=str.encode):
        descriptor = os.dup(root_fd)
        try:
            for component in PurePosixPath(relative).parts:
                child = os.open(
                    component,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            marker = os.open(
                ".pco-empty",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=descriptor,
            )
            try:
                os.write(marker, b"pco retrieval backend directory\n")
                os.fsync(marker)
            finally:
                os.close(marker)
        finally:
            os.close(descriptor)


def _key(document: Mapping[str, Any]) -> str:
    return f"{document['stream']}:{document['id']}@{document['revision']}"


def query_backends(
    generation: Path,
    query: str,
    eligible_keys: set[str],
    candidate_limit: int,
) -> tuple[dict[str, float], dict[str, float]]:
    """Query only backends whose open path is demonstrably read-only.

    The supported Milvus Lite release recreates operational staging state when
    a local database is opened, invalidating the generation it is meant to
    query.  Its deterministic artifact remains part of the rebuildable output,
    while read paths use Tantivy and the in-memory deterministic reranker.
    """

    dense: dict[str, float] = {}
    lexical: dict[str, float] = {}
    if query.strip():
        import tantivy

        index = tantivy.Index.open(str(generation / "tantivy"))
        parsed = index.parse_query(" OR ".join(tokenize(query)), ["text"])
        searcher = index.searcher()
        for score, address in searcher.search(parsed, limit=candidate_limit).hits:
            key = str(searcher.doc(address).get_first("key"))
            if key in eligible_keys:
                lexical[key] = float(score)
    return dense, lexical


def search(
    *,
    repo_root: Path,
    query: str = "",
    mode: str = "current",
    limit: int = 10,
    indexes_root: Path | None = None,
    start: str | None = None,
    end: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Search a verified generation or a bounded exact-commit canonical snapshot."""

    start_at, end_at = _validate_search(query, mode, limit, start, end)
    repository = MemoryRepository(
        Path(repo_root), Profile.load(bundled_profile(), default_registry())
    )
    head = repository.head()
    generation = (
        Path(indexes_root)
        if indexes_root is not None
        else Path(repo_root).parent / "indexes"
    )
    status = "fallback"
    diagnostics: dict[str, Any] | None = None
    backend_scores: tuple[dict[str, float], dict[str, float]] = ({}, {})
    try:
        verified = verify_derivation_tree(
            generation, kind="retrieval_index", expected_commit=head
        )
        if not verified["ok"]:
            raise ValueError(verified["status"])
        documents = _read_json_list(generation / "documents.json")
        reverse_refs = _read_reverse_refs(generation / "reverse_refs.json")
        selected = _select_mode(documents, mode, start_at, end_at)
        backend_scores = query_backends(
            generation,
            query,
            {_key(document) for document in selected},
            min(BACKEND_CANDIDATES, max(limit, 1) * 4),
        )
        after = verify_derivation_tree(
            generation, kind="retrieval_index", expected_commit=head
        )
        if not after["ok"]:
            raise ValueError("generation changed during search")
        status = "current"
    except Exception as exc:
        diagnostics = {
            "code": "DERIVATION_FAILED",
            "message": "retrieval generation unavailable; canonical fallback used",
            "value": str(exc),
        }
        documents, _profile_version = canonical_documents(
            repository, head, canonical_head=head
        )
        reverse_refs = build_reverse_references(documents)
        selected = _select_mode(documents, mode, start_at, end_at)
        selected = _bounded_recent(selected, CANONICAL_FALLBACK_CANDIDATES)
        backend_scores = ({}, {})
    items = _rank(selected, reverse_refs, query, mode, limit, backend_scores)
    result: dict[str, Any] = {
        "ok": True,
        "query": query,
        "mode": mode,
        "source_commit": head,
        "derivation_status": status,
        "candidate_count": len(selected),
        "items": items,
    }
    if diagnostics is not None:
        result["fallback_diagnostic"] = diagnostics
    if mode == "change":
        result["change_windows"] = _change_window_summary(selected)
    return result


def _validate_search(
    query: Any, mode: Any, limit: Any, start: Any, end: Any
) -> tuple[datetime | None, datetime | None]:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if mode not in SEARCH_MODES:
        raise ValueError(f"unknown retrieval mode: {mode}")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or limit > 1000
    ):
        raise ValueError("limit must be an integer from 1 through 1000")
    start_at = _parse_time_bound(start, "start")
    end_at = _parse_time_bound(end, "end")
    if start_at is not None and end_at is not None and start_at > end_at:
        raise ValueError("start must not be later than end")
    return start_at, end_at


def _parse_time_bound(value: Any, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 string") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _record_time(document: Mapping[str, Any]) -> datetime | None:
    value = document.get("occurred_at") or document.get("recorded_at")
    try:
        return _parse_time_bound(value, "record time") if value else None
    except ValueError:
        return None


def _select_mode(
    documents: list[dict[str, Any]],
    mode: str,
    start: datetime | None,
    end: datetime | None,
) -> list[dict[str, Any]]:
    if mode in {"historical", "change"}:
        selected = list(documents)
    else:
        selected = [document for document in documents if document.get("current")]
    if mode == "continuity":
        handoffs = [
            document
            for document in selected
            if document["stream"] == "session_handoffs"
        ]
        selected = [max(handoffs, key=_recent_key)] if handoffs else []
    elif mode == "pattern":
        selected = [
            document
            for document in selected
            if document["stream"]
            in {
                "events",
                "psychologies",
                "philosophies",
                "archetypes",
                "hypotheses",
            }
        ]
    if mode == "current":
        selected = [
            document for document in selected if document.get("status") != "tombstone"
        ]
    result = []
    for document in selected:
        when = _record_time(document)
        if start is not None and (when is None or when < start):
            continue
        if end is not None and (when is None or when > end):
            continue
        result.append(dict(document))
    return sorted(result, key=_document_key)


def _recent_key(document: Mapping[str, Any]) -> tuple[str, int, bytes]:
    return (
        str(document.get("recorded_at") or ""),
        int(document.get("revision") or 0),
        str(document.get("id") or "").encode("utf-8"),
    )


def _bounded_recent(
    documents: list[dict[str, Any]], maximum: int
) -> list[dict[str, Any]]:
    return sorted(
        documents,
        key=lambda item: (_recent_key(item), _document_key(item)),
        reverse=True,
    )[:maximum]


def _rank(
    documents: list[dict[str, Any]],
    reverse_refs: dict[str, list[dict[str, Any]]],
    query: str,
    mode: str,
    limit: int,
    backend_scores: tuple[dict[str, float], dict[str, float]],
) -> list[dict[str, Any]]:
    dense, backend_lexical = backend_scores
    terms = tokenize(query)
    seeds: set[str] = set()
    scored: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for document in documents:
        tokens = tokenize(document.get("text", ""))
        lexical_score = sum(tokens.count(term) for term in terms)
        key = _key(document)
        score = (
            float(lexical_score) + dense.get(key, 0.0) + backend_lexical.get(key, 0.0)
        )
        if query and lexical_score:
            seeds.add(str(document["id"]))
        item = dict(document)
        item["score"] = round(score, 8)
        scored.append(
            (
                (
                    score,
                    float(lexical_score),
                    str(document.get("recorded_at") or ""),
                    int(document.get("revision") or 0),
                ),
                item,
            )
        )
    if mode == "pattern" and seeds:
        linked = {
            edge["source_id"] for seed in seeds for edge in reverse_refs.get(seed, [])
        }
        for index, (sort_key, item) in enumerate(scored):
            if item["id"] in linked:
                item["score"] = round(item["score"] + 0.25, 8)
                scored[index] = ((sort_key[0] + 0.25, *sort_key[1:]), item)
    scored.sort(key=lambda pair: _document_key(pair[1]))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _sort_key, item in scored[:limit]]


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    value = _read_json(path)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("documents.json must contain an array of objects")
    return [dict(item) for item in value]


def _read_reverse_refs(path: Path) -> dict[str, list[dict[str, Any]]]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise ValueError("reverse_refs.json must contain an object")
    result: dict[str, list[dict[str, Any]]] = {}
    for target, edges in value.items():
        if (
            not isinstance(target, str)
            or not isinstance(edges, list)
            or not all(isinstance(edge, dict) for edge in edges)
        ):
            raise ValueError("reverse_refs.json is malformed")
        result[target] = [dict(edge) for edge in edges]
    return result


def _read_json(path: Path) -> Any:
    class StrictObject(dict):
        pass

    def pairs(values):
        result = StrictObject()
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def nonfinite(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=pairs,
        parse_constant=nonfinite,
    )


def _change_window_summary(documents: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        (
            (when, item)
            for item in documents
            if (when := _record_time(item)) is not None
        ),
        key=lambda pair: (pair[0], _document_key(pair[1])),
    )
    if not ordered:
        return {"split_at": None, "earlier": {}, "later": {}}
    split = ordered[len(ordered) // 2][0]
    earlier: Counter[str] = Counter()
    later: Counter[str] = Counter()
    for when, document in ordered:
        (earlier if when <= split else later)[document["stream"]] += 1
    return {
        "split_at": split.isoformat(),
        "earlier": dict(sorted(earlier.items())),
        "later": dict(sorted(later.items())),
    }


# Transitional entry point retained until Task 5 removes the package inventory.
def build_index(
    *,
    repo_root: Path,
    indexes_root: str | Path,
    source_commit: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    repository = MemoryRepository(
        Path(repo_root), Profile.load(bundled_profile(), default_registry())
    )
    commit = source_commit or repository.head()
    manifest = build_retrieval_generation(repository, Path(indexes_root), commit)
    return {"ok": True, **manifest.model_dump(mode="json")}
