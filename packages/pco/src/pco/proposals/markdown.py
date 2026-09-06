"""Strict, deterministic Markdown mapping for reviewable business objects."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from functools import lru_cache
import math
from pathlib import PurePosixPath
import re
from typing import Any, Literal, TypeAlias

from mem_core.errors import MemError
from mem_core.profile import Profile
from mem_core.registry import default_registry
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.tokens import (
    AliasToken,
    AnchorToken,
    BlockEndToken,
    BlockMappingStartToken,
    BlockSequenceStartToken,
    DocumentEndToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
    TagToken,
)

from ..paths import bundled_profile
from .materials import MAX_INPUT_MATERIAL_BYTES, normalize_utf8


BusinessStream: TypeAlias = Literal[
    "evidence",
    "sources",
    "events",
    "psychologies",
    "philosophies",
    "archetypes",
    "hypotheses",
    "meta_revisions",
    "session_handoffs",
]
ReviewOperation: TypeAlias = Literal["create", "revise", "retire"]

_DIRECTORY_TYPES: dict[str, tuple[str, BusinessStream]] = {
    "evidence": ("evidence", "evidence"),
    "sources": ("source", "sources"),
    "events": ("event", "events"),
    "psychologies": ("psychology", "psychologies"),
    "philosophies": ("philosophy", "philosophies"),
    "archetypes": ("archetype", "archetypes"),
    "hypotheses": ("hypothesis", "hypotheses"),
}
_FIXED_PATHS: dict[str, tuple[str, BusinessStream, str | None]] = {
    "META_MEMORY.md": ("meta", "meta_revisions", "meta_current"),
    "SESSION_HANDOFF.md": ("session_handoff", "session_handoffs", None),
}
_STREAM_TYPES: dict[BusinessStream, str] = {
    stream: object_type for object_type, stream in _DIRECTORY_TYPES.values()
}
_STREAM_TYPES.update(
    {"meta_revisions": "meta", "session_handoffs": "session_handoff"}
)
_GENERATED_PAYLOAD_FIELDS = {
    "body_markdown",
    "previous_revision",
    "content_hash",
    "snapshot_ref",
}
_CONTROLS = {"type", "id", "operation", "base_revision"}
_MATERIAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_SEMANTIC_DEPTH = 64
_YAML_CONTAINER_START = (
    BlockMappingStartToken,
    BlockSequenceStartToken,
    FlowMappingStartToken,
    FlowSequenceStartToken,
)
_YAML_CONTAINER_END = (BlockEndToken, FlowMappingEndToken, FlowSequenceEndToken)


class _FrozenMapping(Mapping[str, Any]):
    """An immutable mapping backed only by an immutable tuple of pairs."""

    __slots__ = ("_items",)

    def __init__(self, values: dict[str, Any]) -> None:
        object.__setattr__(self, "_items", tuple(values.items()))

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("parsed proposal semantics are immutable")

    def __getitem__(self, key: str) -> Any:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Mapping):
            return False
        return _deep_thaw(self) == _deep_thaw(other)

    def __repr__(self) -> str:
        return repr(dict(self.items()))


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenMapping(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


def _schema_error(
    message: str,
    *,
    path: PurePosixPath | str | None = None,
    value: Any = None,
    stream: str | None = None,
    record_id: str | None = None,
) -> MemError:
    return MemError(
        "PROPOSAL_SCHEMA_INVALID",
        "proposal_markdown",
        message,
        path=None if path is None else str(path),
        value=value,
        stream=stream,
        record_id=record_id,
        recovery=["Correct the reviewable Markdown frontmatter and validate again"],
        canonical_changed=False,
        retry_safe=False,
    )


def _ensure_bounded_depth(
    value: Any, *, path: PurePosixPath | str
) -> None:
    stack: list[tuple[Any, int, frozenset[int]]] = [(value, 0, frozenset())]
    while stack:
        current, depth, ancestors = stack.pop()
        if not isinstance(current, (dict, list)):
            continue
        if depth > _MAX_SEMANTIC_DEPTH:
            raise _schema_error(
                "Business semantics exceed the maximum nesting depth",
                path=path,
                value={"max_depth": _MAX_SEMANTIC_DEPTH},
            )
        identity = id(current)
        if identity in ancestors:
            raise _schema_error(
                "Business semantics must not contain reference cycles", path=path
            )
        child_ancestors = ancestors | {identity}
        children = current.values() if isinstance(current, dict) else current
        stack.extend((child, depth + 1, child_ancestors) for child in children)


class ParsedBusinessObject(BaseModel):
    """Proposal controls plus authorable, schema-checked business semantics."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True, extra="forbid", strict=True, frozen=True
    )

    stream: BusinessStream
    operation: ReviewOperation
    record_id: str = Field(min_length=1)
    base_revision: int | None
    structured: _FrozenMapping
    body_markdown: str
    relative_path: PurePosixPath
    material_id: str | None = None

    @field_validator("structured", mode="before")
    @classmethod
    def _freeze_structured(cls, value: Any) -> _FrozenMapping:
        if not isinstance(value, dict):
            raise TypeError("structured proposal semantics must be a mapping")
        _ensure_bounded_depth(value, path="<parsed>")
        return _deep_freeze(value)

    @field_serializer("structured")
    def _serialize_structured(self, value: _FrozenMapping) -> dict[str, Any]:
        return _deep_thaw(value)

    def to_record_preview(self) -> dict[str, Any]:
        """Return a generated-field preview accepted by the deterministic renderer."""

        profile = _profile()
        revision = 1 if self.operation == "create" else self.base_revision + 1  # type: ignore[operator]
        payload = _deep_thaw(self.structured)
        payload["previous_revision"] = (
            None if self.operation == "create" else self.base_revision
        )
        payload["body_markdown"] = self.body_markdown
        _add_source_placeholders(self.stream, payload)
        return {
            "id": self.record_id,
            "revision": revision,
            "recorded_at": "2000-01-01T00:00:00+00:00",
            "schema_version": profile.stream(self.stream).schema_version,
            "commit_ref": "commit_preview",
            "payload": payload,
        }


class _StrictLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _StrictLoader, node: MappingNode, deep: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "frontmatter keys must be strings",
                key_node.start_mark,
            )
        if key == "<<":
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "YAML merge keys are forbidden",
                key_node.start_mark,
            )
        if key in result:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


@lru_cache(maxsize=1)
def _profile() -> Profile:
    return Profile.load(bundled_profile(), default_registry())


def _path_contract(path: PurePosixPath) -> tuple[str, BusinessStream, str | None]:
    raw = path.as_posix()
    if path.is_absolute() or raw in {"", "."} or ".." in path.parts:
        raise _schema_error("Business Markdown path must be normalized and relative", path=path)
    fixed = _FIXED_PATHS.get(raw)
    if fixed is not None:
        return fixed
    if len(path.parts) != 2 or path.suffix != ".md":
        raise _schema_error("Business Markdown path does not match the proposal tree", path=path)
    mapping = _DIRECTORY_TYPES.get(path.parts[0])
    if mapping is None:
        raise _schema_error("Business Markdown path uses an unknown stream", path=path)
    object_type, stream = mapping
    return object_type, stream, path.stem


def _frontmatter_and_body(data: bytes, path: PurePosixPath) -> tuple[dict[str, Any], str]:
    try:
        normalized = normalize_utf8([data], limit=MAX_INPUT_MATERIAL_BYTES)
    except (MemError, TypeError, ValueError) as exc:
        raise _schema_error("Business Markdown is not normalized strict UTF-8", path=path) from exc
    lines = normalized.splitlines(keepends=True)
    if not lines or lines[0] != b"---\n":
        raise _schema_error("Business Markdown must begin with an exact --- delimiter", path=path)
    terminator = next(
        (index for index, line in enumerate(lines[1:], start=1) if line == b"---\n"),
        None,
    )
    if terminator is None:
        raise _schema_error("Business Markdown frontmatter has no exact terminating delimiter", path=path)
    frontmatter_bytes = b"".join(lines[1:terminator])
    body_bytes = b"".join(lines[terminator + 1 :])
    try:
        nesting_depth = 0
        for token in yaml.scan(frontmatter_bytes.decode("utf-8")):
            if isinstance(token, (AliasToken, AnchorToken, TagToken, DocumentEndToken)):
                raise _schema_error(
                    "YAML aliases, anchors, tags, merge keys, and multiple documents are forbidden",
                    path=path,
                )
            if isinstance(token, _YAML_CONTAINER_START):
                nesting_depth += 1
                if nesting_depth > _MAX_SEMANTIC_DEPTH:
                    raise _schema_error(
                        "Business semantics exceed the maximum nesting depth",
                        path=path,
                        value={"max_depth": _MAX_SEMANTIC_DEPTH},
                    )
            elif isinstance(token, _YAML_CONTAINER_END):
                nesting_depth -= 1
        loaded = yaml.load(frontmatter_bytes.decode("utf-8"), Loader=_StrictLoader)
    except MemError:
        raise
    except RecursionError as exc:
        raise _schema_error(
            "Business Markdown nesting exceeds the parser limit", path=path
        ) from exc
    except yaml.YAMLError as exc:
        raise _schema_error("Business Markdown frontmatter is invalid YAML", path=path) from exc
    if not isinstance(loaded, dict):
        raise _schema_error("Business Markdown frontmatter must be one mapping", path=path)
    _ensure_bounded_depth(loaded, path=path)
    _require_json_values(loaded, path=path)
    body = body_bytes.decode("utf-8")
    if not body:
        raise _schema_error("Business Markdown body must be non-empty", path=path)
    return loaded, body


def _require_json_values(value: Any, *, path: PurePosixPath, pointer: str = "") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_values(item, path=path, pointer=f"{pointer}/{index}")
        return
    elif isinstance(value, dict):
        if all(isinstance(key, str) and key != "<<" for key in value):
            for key, item in value.items():
                _require_json_values(item, path=path, pointer=f"{pointer}/{key}")
            return
    raise _schema_error(
        "Frontmatter contains a non-JSON or unsafe YAML value",
        path=path,
        value={"pointer": pointer, "type": type(value).__name__},
    )


def _deterministic_yaml_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _deterministic_yaml_value(value[key])
            for key in sorted(value, key=lambda item: item.encode("utf-8"))
        }
    if isinstance(value, list):
        return [_deterministic_yaml_value(item) for item in value]
    return value


def _payload_fields(stream: BusinessStream) -> set[str]:
    validator = _profile().schema_validator(stream)
    properties = validator.schema["properties"]["payload"]["properties"]
    return set(properties) - _GENERATED_PAYLOAD_FIELDS


def _validate_controls(
    frontmatter: dict[str, Any],
    *,
    expected_type: str,
    expected_id: str | None,
    stream: BusinessStream,
    path: PurePosixPath,
) -> tuple[ReviewOperation, str, int | None]:
    object_type = frontmatter.get("type")
    record_id = frontmatter.get("id")
    operation = frontmatter.get("operation")
    base_revision = frontmatter.get("base_revision")
    if object_type != expected_type:
        raise _schema_error(
            f"Path requires frontmatter type {expected_type!r}", path=path, value=object_type
        )
    if (
        not isinstance(record_id, str)
        or not record_id
        or (expected_id is not None and record_id != expected_id)
    ):
        raise _schema_error("Frontmatter id does not match the business filename", path=path, value=record_id)
    if operation not in {"create", "revise", "retire"}:
        raise _schema_error("Frontmatter operation must be create, revise, or retire", path=path, value=operation)
    if operation == "create":
        if "base_revision" in frontmatter:
            raise _schema_error("Create operations must omit base_revision", path=path)
        base_revision = None
    elif not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 1:
        raise _schema_error("Revise and retire operations require a positive base_revision", path=path, value=base_revision)
    return operation, record_id, base_revision


def _add_source_placeholders(stream: BusinessStream, payload: dict[str, Any]) -> None:
    if stream == "sources":
        payload.setdefault("content_hash", "sha256:" + "0" * 64)
        payload.setdefault("snapshot_ref", "source_blobs/" + "0" * 64)


def _validate_reference_shapes(stream: BusinessStream, structured: dict[str, Any], path: PurePosixPath) -> None:
    for field in ("evidence_refs", "counter_evidence_refs"):
        refs = structured.get(field, [])
        if not isinstance(refs, list):
            continue
        for reference in refs:
            if not isinstance(reference, str) or not reference.startswith("evidence:") or not reference[9:]:
                raise _schema_error(f"{field} contains an invalid evidence reference", path=path, value=reference, stream=stream)
    refs = structured.get("promotion_refs", [])
    if isinstance(refs, list):
        for reference in refs:
            if not isinstance(reference, str) or not reference.startswith("hypothesis:") or not reference[11:]:
                raise _schema_error("promotion_refs contains an invalid hypothesis reference", path=path, value=reference, stream=stream)


def _validate_schema(
    stream: BusinessStream,
    operation: ReviewOperation,
    record_id: str,
    base_revision: int | None,
    structured: dict[str, Any],
    body: str,
    path: PurePosixPath,
) -> None:
    status = structured.get("status")
    if operation == "retire" and status != "tombstone":
        raise _schema_error("Retire operations require tombstone status", path=path, value=status)
    if operation != "retire" and status == "tombstone":
        raise _schema_error("Tombstone status requires a retire operation", path=path, value=status)
    revision = 1 if operation == "create" else base_revision + 1  # type: ignore[operator]
    payload = dict(structured)
    payload["previous_revision"] = None if operation == "create" else base_revision
    payload["body_markdown"] = body
    _add_source_placeholders(stream, payload)
    record = {
        "id": record_id,
        "revision": revision,
        "recorded_at": "2000-01-01T00:00:00+00:00",
        "schema_version": _profile().stream(stream).schema_version,
        "commit_ref": "commit_preview",
        "payload": payload,
    }
    try:
        _profile().validate_record_schema(stream, record)
    except MemError as exc:
        raise _schema_error(
            "Business Markdown does not satisfy the Profile schema",
            path=path,
            value=exc.as_dict()["error"],
            stream=stream,
            record_id=record_id,
        ) from exc


def parse_business_markdown(path: PurePosixPath, data: bytes) -> ParsedBusinessObject:
    """Parse one strictly bounded reviewable business Markdown file."""

    if not isinstance(path, PurePosixPath) or not isinstance(data, bytes):
        raise _schema_error("Business Markdown requires a POSIX path and exact bytes")
    expected_type, stream, expected_id = _path_contract(path)
    frontmatter, body = _frontmatter_and_body(data, path)
    operation, record_id, base_revision = _validate_controls(
        frontmatter,
        expected_type=expected_type,
        expected_id=expected_id,
        stream=stream,
        path=path,
    )
    allowed = _CONTROLS | _payload_fields(stream)
    if stream == "sources":
        allowed.add("material_id")
    unknown = sorted(set(frontmatter) - allowed)
    if unknown:
        raise _schema_error("Frontmatter contains unknown or generated fields", path=path, value=unknown)
    structured = {
        key: value
        for key, value in frontmatter.items()
        if key not in _CONTROLS and key != "material_id"
    }
    material_id = frontmatter.get("material_id") if stream == "sources" else None
    if material_id is not None:
        if not isinstance(material_id, str) or not _MATERIAL_ID.fullmatch(material_id):
            raise _schema_error("Source material_id must be a stable ASCII identifier", path=path, value=material_id)
    _validate_reference_shapes(stream, structured, path)
    _validate_schema(stream, operation, record_id, base_revision, structured, body, path)
    return ParsedBusinessObject(
        stream=stream,
        operation=operation,
        record_id=record_id,
        base_revision=base_revision,
        structured=structured,
        body_markdown=body,
        relative_path=path,
        material_id=material_id,
    )


def _stream_for_record(record: dict[str, Any]) -> BusinessStream:
    schema_version = record.get("schema_version")
    matches = [
        stream
        for stream in _STREAM_TYPES
        if _profile().stream(stream).schema_version == schema_version
    ]
    if len(matches) != 1:
        raise _schema_error("Record schema_version does not identify one business stream", value=schema_version)
    return matches[0]


def render_business_markdown(record: dict[str, Any]) -> bytes:
    """Render a canonical or preview record as deterministic normalized Markdown."""

    if not isinstance(record, dict):
        raise _schema_error("Business record must be a mapping")
    stream = _stream_for_record(record)
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise _schema_error("Business record payload must be a mapping", stream=stream)
    _ensure_bounded_depth(record, path="<record>")
    _require_json_values(record, path=PurePosixPath("<record>"))
    try:
        _profile().validate_record_schema(stream, record)
    except MemError as exc:
        raise _schema_error(
            "Business record does not satisfy the Profile schema",
            value=exc.as_dict()["error"],
            stream=stream,
            record_id=record.get("id"),
        ) from exc
    except RecursionError as exc:
        raise _schema_error(
            "Business record nesting exceeds the schema limit",
            stream=stream,
            record_id=record.get("id"),
        ) from exc
    revision = record.get("revision")
    previous = payload.get("previous_revision")
    if revision == 1:
        if previous is not None:
            raise _schema_error(
                "Initial records must not identify a previous revision",
                stream=stream,
                record_id=record.get("id"),
            )
    elif previous != revision - 1:
        raise _schema_error(
            "Revised records must identify the immediately previous revision",
            stream=stream,
            record_id=record.get("id"),
        )
    status = payload.get("status")
    if status == "tombstone":
        operation: ReviewOperation = "retire"
    elif revision == 1:
        operation = "create"
    else:
        operation = "revise"
    frontmatter: dict[str, Any] = {
        "type": _STREAM_TYPES[stream],
        "id": record["id"],
        "operation": operation,
    }
    if operation != "create":
        if not isinstance(previous, int) or isinstance(previous, bool):
            raise _schema_error("Revised records require previous_revision", stream=stream, record_id=record.get("id"))
        frontmatter["base_revision"] = previous
    for key in _profile().schema_validator(stream).schema["properties"]["payload"]["properties"]:
        if key not in _GENERATED_PAYLOAD_FIELDS and key in payload:
            frontmatter[key] = _deterministic_yaml_value(payload[key])
    try:
        yaml_text = yaml.dump(
            frontmatter,
            Dumper=_NoAliasDumper,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            explicit_start=False,
            explicit_end=False,
            width=10_000,
        )
        header = b"---\n" + yaml_text.encode("utf-8") + b"---\n"
        body = normalize_utf8(
            [payload["body_markdown"].encode("utf-8")], limit=MAX_INPUT_MATERIAL_BYTES
        )
        rendered = header + body
        return normalize_utf8([rendered], limit=MAX_INPUT_MATERIAL_BYTES)
    except (
        KeyError,
        AttributeError,
        UnicodeError,
        MemError,
        RecursionError,
        TypeError,
        ValueError,
    ) as exc:
        if isinstance(exc, MemError) and exc.detail.code == "PROPOSAL_SCHEMA_INVALID":
            raise
        raise _schema_error("Business record cannot be rendered safely", stream=stream, record_id=record.get("id")) from exc
