from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, Field

from .errors import MemError, ensure


class StreamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str
    schema_path: str = Field(alias="schema")
    schema_version: str
    write_policy: str = Field(pattern="^(auto|user_approval|read_only)$")
    approval_ref_pointer: str | None = None


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    version: str
    streams: dict[str, StreamConfig]
    validators: list[str] = Field(default_factory=list)
    artifact_roots: list[str] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)


ValidatorCallable = Callable[[Path, dict[str, list[dict[str, Any]]]], Iterable[dict[str, Any]] | None]
CapabilityCallable = Callable[..., Any]


class ProfileRegistry:
    """Explicit allowlist for Python behavior referenced by Profile YAML."""

    def __init__(self) -> None:
        self._entries: dict[str, Any] = {}

    def register(self, name: str, target: Any) -> None:
        ensure(name not in self._entries, "ENTRYPOINT_DUPLICATE", "profile_load", f"Duplicate entry point: {name}")
        self._entries[name] = target

    def register_lazy(self, name: str, target: str) -> None:
        ensure(":" in target, "ENTRYPOINT_INVALID", "profile_load", f"Invalid entry point: {target}")
        self._entries[name] = target

    def resolve(self, name: str) -> Any:
        ensure(
            name in self._entries,
            "ENTRYPOINT_NOT_ALLOWED",
            "profile_load",
            f"Profile entry point is not allowlisted: {name}",
            value=name,
            recovery=["Register the entry point in the application allowlist"],
        )
        target = self._entries[name]
        if isinstance(target, str):
            module_name, attr = target.split(":", 1)
            target = getattr(importlib.import_module(module_name), attr)
            self._entries[name] = target
        return target


@dataclass(slots=True)
class Profile:
    root: Path
    config: ProfileConfig
    registry: ProfileRegistry
    raw: dict[str, Any]
    _schema_validators: dict[str, Draft202012Validator] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def load(cls, root: Path, registry: ProfileRegistry | None = None) -> "Profile":
        root = root.resolve()
        config_path = root / "profile.yaml"
        ensure(config_path.is_file(), "PROFILE_NOT_FOUND", "profile_load", f"Missing {config_path}")
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config = ProfileConfig.model_validate(raw)
        except Exception as exc:
            raise MemError("PROFILE_INVALID", "profile_load", str(exc), path=str(config_path)) from exc
        result = cls(root=root, config=config, registry=registry or ProfileRegistry(), raw=raw)
        result._validate_paths_and_entrypoints()
        return result

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def version(self) -> str:
        return self.config.version

    @property
    def policy_hash(self) -> str:
        encoded = json.dumps(self.raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def _safe_relative(self, raw_path: str, *, phase: str = "profile_load") -> Path:
        path = Path(raw_path)
        ensure(not path.is_absolute(), "PROFILE_PATH_UNSAFE", phase, "Profile paths must be relative", value=raw_path)
        ensure(".." not in path.parts, "PROFILE_PATH_UNSAFE", phase, "Profile paths cannot escape their root", value=raw_path)
        return path

    def _validate_paths_and_entrypoints(self) -> None:
        seen_paths: set[str] = set()
        for stream_name, stream in self.config.streams.items():
            self._safe_relative(stream.path)
            self._safe_relative(stream.schema_path)
            ensure(stream.path not in seen_paths, "STREAM_PATH_DUPLICATE", "profile_load", f"Duplicate stream path: {stream.path}")
            seen_paths.add(stream.path)
            ensure((self.root / stream.schema_path).is_file(), "SCHEMA_NOT_FOUND", "profile_load", f"Missing schema for {stream_name}", path=stream.schema_path)
        for root in self.config.artifact_roots:
            self._safe_relative(root)
        for entry in self.config.validators:
            self.registry.resolve(entry)
        for entry in self.iter_capability_entrypoints():
            self.registry.resolve(entry)

    def iter_capability_entrypoints(self) -> Iterable[str]:
        def walk(value: Any) -> Iterable[str]:
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for nested in value.values():
                    yield from walk(nested)

        yield from walk(self.config.capabilities)

    def stream(self, name: str) -> StreamConfig:
        try:
            return self.config.streams[name]
        except KeyError as exc:
            raise MemError("STREAM_NOT_FOUND", "profile_validation", f"Unknown stream: {name}", stream=name) from exc

    def stream_path(self, repo_root: Path, name: str) -> Path:
        return repo_root / self._safe_relative(self.stream(name).path)

    def schema_validator(self, name: str) -> Draft202012Validator:
        cached = self._schema_validators.get(name)
        if cached is not None:
            return cached
        stream = self.stream(name)
        schema = json.loads((self.root / stream.schema_path).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        self._schema_validators[name] = validator
        return validator

    def validate_record_schema(self, stream_name: str, record: dict[str, Any]) -> None:
        stream = self.stream(stream_name)
        ensure(
            record.get("schema_version") == stream.schema_version,
            "SCHEMA_VERSION_MISMATCH",
            "profile_validation",
            f"Expected {stream.schema_version}",
            stream=stream_name,
            record_id=record.get("id"),
            path="/schema_version",
            value=record.get("schema_version"),
        )
        validator = self.schema_validator(stream_name)
        if validator.is_valid(record):
            return
        errors = sorted(validator.iter_errors(record), key=lambda e: list(e.absolute_path))
        if errors:
            error = errors[0]
            pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path)
            raise MemError(
                "SCHEMA_VALIDATION_FAILED",
                "profile_validation",
                error.message,
                stream=stream_name,
                record_id=record.get("id"),
                path=pointer,
                retryable=True,
                recovery=["Correct the record at the reported JSON Pointer and validate again"],
            )

    def artifact_path(self, repo_root: Path, raw_path: str) -> Path:
        relative = self._safe_relative(raw_path, phase="transaction_validation")
        allowed = any(relative == Path(root) or Path(root) in relative.parents for root in self.config.artifact_roots)
        ensure(
            allowed,
            "ARTIFACT_PATH_NOT_ALLOWED",
            "transaction_validation",
            f"Artifact path is outside Profile allowlist: {raw_path}",
            value=raw_path,
        )
        return repo_root / relative

    def invoke(self, name: str, **kwargs: Any) -> Any:
        node: Any = self.config.capabilities
        for part in name.split("."):
            ensure(isinstance(node, dict) and part in node, "CAPABILITY_NOT_FOUND", "profile_invoke", f"Unknown capability: {name}")
            node = node[part]
        ensure(isinstance(node, str), "CAPABILITY_NOT_CALLABLE", "profile_invoke", f"Capability is a group: {name}")
        target = self.registry.resolve(node)
        if isinstance(target, type):
            target = target()
        return target(**kwargs)

    def run_validators(self, repo_root: Path, records: dict[str, list[dict[str, Any]]]) -> None:
        for name in self.config.validators:
            validator: ValidatorCallable = self.registry.resolve(name)
            problems = validator(repo_root, records)
            problem = next(iter(problems or ()), None)
            if problem is not None:
                raise MemError(**problem)
