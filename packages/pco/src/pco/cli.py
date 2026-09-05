"""Public v0.5 workspace command-line interface."""

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path
from typing import Any

from mem_core.errors import MemError

from .verify import verify_workspace
from .workspace import Workspace


def build_parser() -> argparse.ArgumentParser:
    """Build the deliberately small v0.5 command surface without side effects."""
    parser = argparse.ArgumentParser(prog="pco")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("path", type=Path, nargs="?", default=None)
    commands.add_parser("doctor")
    verify = commands.add_parser("verify")
    verify.add_argument("--path", type=Path)
    return parser


def _is_safe_workspace_marker(root: Path) -> bool:
    """Return whether ``root`` has the fixed marker without following links."""
    try:
        pco = root / ".pco"
        pco_info = pco.lstat()
        if stat.S_ISLNK(pco_info.st_mode) or not stat.S_ISDIR(pco_info.st_mode):
            return False

        canonical = pco / "canonical"
        canonical_info = canonical.lstat()
        if (
            stat.S_ISLNK(canonical_info.st_mode)
            or not stat.S_ISDIR(canonical_info.st_mode)
        ):
            return False

        identity = canonical / "workspace.json"
        identity_info = identity.lstat()
        return not stat.S_ISLNK(identity_info.st_mode) and stat.S_ISREG(
            identity_info.st_mode
        )
    except OSError:
        return False


def _discover_workspace(start: Path | None = None) -> Path:
    """Find the nearest ancestor with a safe fixed workspace marker."""
    current = (start or Path.cwd()).absolute()
    searched_from = current
    while True:
        if _is_safe_workspace_marker(current):
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise MemError(
        "WORKSPACE_NOT_INITIALIZED",
        "workspace",
        f"Not a PCO workspace above: {searched_from}",
        canonical_changed=False,
        retry_safe=False,
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Dispatch parsed workspace commands through the public workspace APIs."""
    if args.command == "init":
        return Workspace.init(args.path if args.path is not None else Path.cwd())

    workspace = Workspace.open(_discover_workspace())
    if args.command == "doctor":
        return workspace.doctor()
    if args.command == "verify":
        path = args.path
        if path is not None and not path.is_absolute():
            path = workspace.root / path
        return verify_workspace(workspace, path=path)
    raise MemError(
        "COMMAND_INVALID",
        "cli",
        "Unsupported command",
        canonical_changed=False,
        retry_safe=False,
    )


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a shell-compatible status without raising MemError."""
    try:
        result = run(build_parser().parse_args(argv))
    except MemError as exc:
        _emit(exc.as_dict())
        return 1
    except ValueError as exc:
        _emit(
            MemError(
                "INVALID_INPUT",
                "cli",
                str(exc),
                canonical_changed=False,
                retry_safe=False,
            ).as_dict()
        )
        return 1
    except Exception as exc:
        _emit(
            MemError(
                "UNEXPECTED",
                "cli",
                str(exc),
                canonical_changed=False,
                retry_safe=False,
            ).as_dict()
        )
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
