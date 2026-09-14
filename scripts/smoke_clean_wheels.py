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
import os
from pathlib import Path
import sysconfig

import mem_core
import pco
from mem_core.profile import Profile
from mem_core.registry import default_registry
from pco.paths import bundled_profile


def require_installed_origin(module):
    origin = Path(module.__file__).resolve(strict=True)
    install_roots = {
        Path(path).resolve(strict=True)
        for key, path in sysconfig.get_paths().items()
        if key in {"purelib", "platlib"}
    }
    normalized_origin = os.path.normcase(str(origin))
    for root in install_roots:
        normalized_root = os.path.normcase(str(root))
        try:
            if os.path.commonpath((normalized_origin, normalized_root)) == normalized_root:
                return
        except ValueError:
            continue
    raise AssertionError(f"{module.__name__} origin outside fresh environment: {origin}")


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
require_installed_origin(mem_core)
require_installed_origin(pco)
resources = files("pco.resources")
profile_files = regular_files(resources.joinpath("profiles", "pco"))
assert "profile.yaml" in profile_files
assert any(path.startswith("schemas/") for path in profile_files)

profile = Profile.load(bundled_profile(), default_registry())
assert profile.name == "pco"
assert profile.version == "0.5.0"
assert set(profile.config.streams) == {
    "evidence",
    "sources",
    "events",
    "psychologies",
    "philosophies",
    "archetypes",
    "hypotheses",
    "meta_revisions",
    "session_handoffs",
}

skills = resources.joinpath("skills")
for name in ("pco-memory", "pco-consolidate"):
    members = regular_files(skills.joinpath(name))
    assert {"SKILL.md", "PERMISSIONS.md", "manifest.json"} <= members
"""


_UNSAFE_CHILD_ENVIRONMENT = {
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONUSERBASE",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "PYTHONEXECUTABLE",
    "__PYVENV_LAUNCHER__",
    "VIRTUAL_ENV",
}


def sanitized_environment() -> dict[str, str]:
    """Copy the caller environment without Python/venv source injection."""

    result = {
        key: value
        for key, value in os.environ.items()
        if key not in _UNSAFE_CHILD_ENVIRONMENT
    }
    result["PYTHONNOUSERSITE"] = "1"
    return result


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
        wheelhouse = Path(wheel_dir).resolve()
        environment = Path(env_dir).resolve()
        if wheelhouse.is_relative_to(repo_root) or environment.is_relative_to(
            repo_root
        ):
            raise RuntimeError("smoke temporary directories must be outside repository")
        child_env = sanitized_environment()
        child_cwd = str(wheelhouse)
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
            env=child_env,
            cwd=child_cwd,
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
            env=child_env,
            cwd=child_cwd,
        )
        venv.EnvBuilder(with_pip=True).create(env_dir)
        python, scripts = environment_commands(environment)
        wheels = exact_wheels(wheelhouse, names=("mem_core", "pco"), version="0.5.0")
        run(
            [str(python), "-m", "pip", "install", *map(str, wheels)],
            check=True,
            env=child_env,
            cwd=child_cwd,
        )
        run(
            [str(python), "-c", RESOURCE_AND_VERSION_PROBE],
            check=True,
            env=child_env,
            cwd=child_cwd,
        )
        run(
            [str(scripts / executable_name("pco")), "--help"],
            check=True,
            env=child_env,
            cwd=child_cwd,
        )
        run(
            [str(scripts / executable_name("mem")), "--help"],
            check=True,
            env=child_env,
            cwd=child_cwd,
        )


def main() -> int:
    smoke(Path(__file__).resolve().parents[1])
    print("clean-wheel smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
