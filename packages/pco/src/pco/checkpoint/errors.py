from __future__ import annotations

from typing import Any

from mem_core.errors import MemError
from mem_core.models import utc_now


_RECOVERY: dict[str, list[str]] = {
    "index": ["Verify the index backend and retry derivations", "Run pco derive index --force"],
    "backlinks": ["Verify the canonical memory and retry derivations", "Run pco derive backlinks"],
    "projection": ["Verify the configured projection capability and retry derivations"],
    "context": ["Verify the context renderer and retry context publication"],
    "worker_cleanup": ["Retry derivations to clean up the worker"],
}


def json_compatible(value: Any) -> Any:
    """Normalize arbitrary capability details before persisting runtime state."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_compatible(item) for item in value]
    return str(value)


def derivation_error(exc: Exception, phase: str) -> dict[str, Any]:
    """Normalize a derivation failure without losing MemError details."""
    if isinstance(exc, MemError):
        return json_compatible(exc.as_dict()["error"])
    return json_compatible(MemError(
        "UNEXPECTED_DERIVATION_FAILURE",
        phase,
        str(exc),
        retryable=True,
        recovery=_RECOVERY.get(phase, ["Retry the derivation from its durable boundary"]),
    ).as_dict()["error"])


def structured_error(value: Any, phase: str) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("code"):
        return json_compatible({
            "code": value["code"],
            "phase": value.get("phase", phase),
            "message": value.get("message", value["code"]),
            "retryable": bool(value.get("retryable", True)),
            "recovery": list(value.get("recovery", _RECOVERY.get(phase, []))),
            **{key: value[key] for key in ("stream", "record_id", "path", "value") if key in value},
        })
    return derivation_error(RuntimeError(str(value)), phase)


def failed_attempt(previous: dict[str, Any], exc: Exception, phase: str) -> dict[str, Any]:
    error = json_compatible(derivation_error(exc, phase))
    attempts = list(previous.get("attempts", []))
    attempts.append({"attempt": len(attempts) + 1, "at": utc_now(), "error": json_compatible(error)})
    return {
        "ok": False,
        "pending": True,
        "error": json_compatible(previous.get("error", error)),
        "attempts": json_compatible(attempts),
    }


def successful_attempt(previous: dict[str, Any], result: dict[str, Any] | None = None) -> dict[str, Any]:
    attempts = list(previous.get("attempts", []))
    if previous.get("error") or attempts:
        attempts.append(
            {
                "attempt": len(attempts) + 1,
                "at": utc_now(),
                "recovered_from": json_compatible(previous.get("error")),
                "result": json_compatible(result or {}),
            }
        )
    output = {key: value for key, value in previous.items() if key not in {"ok", "pending", "attempts"}}
    output = json_compatible(output)
    output.update(json_compatible(result or {}))
    output["ok"] = True
    if attempts:
        output["attempts"] = json_compatible(attempts)
    return output


def result_attempt(previous: dict[str, Any], result: dict[str, Any], phase: str) -> dict[str, Any]:
    """Record a capability result, including backends that return ok=false."""
    if result.get("ok", True) is False:
        error = json_compatible(structured_error(result.get("error") or result, phase))
        attempts = list(previous.get("attempts", []))
        attempts.append({"attempt": len(attempts) + 1, "at": utc_now(), "error": error})
        return {"ok": False, "pending": True, "error": json_compatible(previous.get("error", error)), "attempts": json_compatible(attempts)}
    return successful_attempt(previous, result)
