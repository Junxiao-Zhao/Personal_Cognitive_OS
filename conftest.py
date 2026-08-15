"""Root pytest configuration for the monorepo.

The two distributions are kept under packages/ so in-process imports use the
``pythonpath`` setting in pyproject.toml.  Git pre-commit hooks and other
subprocesses do not inherit pytest's ``sys.path``, so expose both package
source roots through ``PYTHONPATH`` here.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_MEM_CORE_SRC = ROOT / "packages" / "mem-core" / "src"
_PCO_SRC = ROOT / "packages" / "pco" / "src"


def pytest_configure() -> None:
    paths = [str(_MEM_CORE_SRC), str(_PCO_SRC)]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        paths.append(existing)
    os.environ["PYTHONPATH"] = os.pathsep.join(paths)
