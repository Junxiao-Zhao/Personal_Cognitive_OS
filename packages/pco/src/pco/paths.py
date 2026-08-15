from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def bundled_profile(name: str = "pco") -> Path:
    return Path(str(files("pco.resources.profiles").joinpath(name)))
