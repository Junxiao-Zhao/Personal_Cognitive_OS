from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ErrorDetail:
    code: str
    phase: str
    message: str
    canonical_changed: bool = False
    retry_safe: bool = False
    stream: str | None = None
    record_id: str | None = None
    path: str | None = None
    value: Any = None
    recovery: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "phase": self.phase,
            "message": self.message,
            "canonical_changed": self.canonical_changed,
            "retry_safe": self.retry_safe,
            "recovery": self.recovery,
        }
        for key in ("stream", "record_id", "path", "value"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


class MemError(Exception):
    """Stable structured error returned by every public mem-core boundary."""

    def __init__(
        self,
        code: str,
        phase: str,
        message: str,
        *,
        canonical_changed: bool,
        retry_safe: bool,
        stream: str | None = None,
        record_id: str | None = None,
        path: str | None = None,
        value: Any = None,
        recovery: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.detail = ErrorDetail(
            code=code,
            phase=phase,
            message=message,
            canonical_changed=canonical_changed,
            retry_safe=retry_safe,
            stream=stream,
            record_id=record_id,
            path=path,
            value=value,
            recovery=recovery or [],
        )

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.detail.as_dict()}


def ensure(
    condition: bool,
    code: str,
    phase: str,
    message: str,
    *,
    canonical_changed: bool,
    retry_safe: bool,
    **details: Any,
) -> None:
    if not condition:
        raise MemError(
            code,
            phase,
            message,
            canonical_changed=canonical_changed,
            retry_safe=retry_safe,
            **details,
        )
