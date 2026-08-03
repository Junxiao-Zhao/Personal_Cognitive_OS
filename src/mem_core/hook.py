from __future__ import annotations

import argparse
import json
from pathlib import Path

from .errors import MemError
from .profile import Profile
from .registry import default_registry
from .repository import MemoryRepository


def validate_repository(repo_root: Path) -> dict[str, object]:
    root = repo_root.resolve()
    marker_path = root / ".mem-profile.json"
    if not marker_path.is_file():
        raise MemError("PROFILE_MARKER_NOT_FOUND", "pre_commit", f"Missing {marker_path}")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        raw_profile_path = Path(marker["path"])
    except Exception as exc:
        raise MemError("PROFILE_MARKER_INVALID", "pre_commit", str(exc), path=str(marker_path)) from exc
    profile_path = raw_profile_path if raw_profile_path.is_absolute() else root / raw_profile_path
    profile = Profile.load(profile_path, default_registry())
    if profile.name != marker.get("name") or profile.version != marker.get("version"):
        raise MemError(
            "PROFILE_MARKER_MISMATCH",
            "pre_commit",
            "Canonical Profile does not match .mem-profile.json",
            path=str(marker_path),
        )
    return MemoryRepository(root, profile).validate_all(root=root)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m mem_core.hook")
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = validate_repository(args.repo)
    except MemError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
