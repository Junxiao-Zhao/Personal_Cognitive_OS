#!/usr/bin/env python3
"""Build and smoke-test the two PCO distributions in a fresh environment."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from subprocess import CompletedProcess
import sys
from tempfile import TemporaryDirectory
from typing import Callable, Sequence
import venv

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

RESOURCE_AND_VERSION_PROBE = r"""
from importlib.resources import files

import mem_core
import pco


def regular_files(root, prefix=""):
    result = set()
    for child in root.iterdir():
        relative = f"{prefix}/{child.name}" if prefix else child.name
        if child.is_dir():
            result.update(regular_files(child, relative))
        elif child.is_file():
            result.add(relative)
    return result


assert mem_core.__version__ == pco.__version__ == "0.5.0"
resources = files("pco.resources")
profile_files = regular_files(resources.joinpath("profiles", "pco"))
assert "profile.yaml" in profile_files
assert any(path.startswith("schemas/") for path in profile_files)

skills = resources.joinpath("skills")
for name in ("pco-memory", "pco-consolidate"):
    members = regular_files(skills.joinpath(name))
    assert {"SKILL.md", "PERMISSIONS.md", "manifest.json"} <= members
"""


def executable_name(name: str) -> str:
    """Return a virtual-environment executable name for this platform."""

    return f"{name}.exe" if os.name == "nt" else name


def environment_commands(environment: Path) -> tuple[Path, Path]:
    """Resolve the interpreter and console-script directory in one venv."""

    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    return scripts / executable_name("python"), scripts


def exact_wheels(
    wheelhouse: Path, *, names: Sequence[str], version: str
) -> tuple[Path, ...]:
    """Resolve exactly one wheel for each requested distribution and version."""

    expected_version = Version(version)
    parsed: list[tuple[Path, str, Version]] = []
    for wheel in sorted(wheelhouse.glob("*.whl")):
        try:
            distribution, observed_version, _build, _tags = parse_wheel_filename(
                wheel.name
            )
        except ValueError as exc:
            raise RuntimeError(f"invalid wheel filename: {wheel.name}") from exc
        parsed.append((wheel, canonicalize_name(distribution), observed_version))

    resolved: list[Path] = []
    for name in names:
        matches = [
            wheel
            for wheel, distribution, observed_version in parsed
            if distribution == canonicalize_name(name)
            and observed_version == expected_version
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected exactly one {name}=={version} wheel, found {len(matches)}"
            )
        resolved.append(matches[0])
    return tuple(resolved)


def smoke(
    repo_root: Path,
    *,
    run: Callable[..., CompletedProcess] = subprocess.run,
) -> None:
    """Build, install, import, enumerate resources, and invoke both CLIs."""

    repo_root = Path(repo_root).resolve()
    with TemporaryDirectory(prefix="pco-wheelhouse-") as wheel_dir, TemporaryDirectory(
        prefix="pco-venv-"
    ) as env_dir:
        wheelhouse = Path(wheel_dir)
        run(
            [
                sys.executable,
                "-m",
                "build",
                str(repo_root / "packages/mem-core"),
                "--wheel",
                "--outdir",
                str(wheelhouse),
            ],
            check=True,
        )
        run(
            [
                sys.executable,
                "-m",
                "build",
                str(repo_root / "packages/pco"),
                "--wheel",
                "--outdir",
                str(wheelhouse),
            ],
            check=True,
        )
        venv.EnvBuilder(with_pip=True).create(env_dir)
        python, scripts = environment_commands(Path(env_dir))
        wheels = exact_wheels(wheelhouse, names=("mem_core", "pco"), version="0.5.0")
        run(
            [str(python), "-m", "pip", "install", *map(str, wheels)],
            check=True,
        )
        run([str(python), "-c", RESOURCE_AND_VERSION_PROBE], check=True)
        run([str(scripts / executable_name("pco")), "--help"], check=True)
        run([str(scripts / executable_name("mem")), "--help"], check=True)


def main() -> int:
    smoke(Path(__file__).resolve().parents[1])
    print("clean-wheel smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
