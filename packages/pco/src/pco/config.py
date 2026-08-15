from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Literal

from hydra import compose, initialize_config_dir
from omegaconf import OmegaConf
from pydantic import BaseModel, ConfigDict, Field


class DerivationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: bool = True
    backlinks: bool = True
    projection: Literal["affine", "markdown", "none"] = "affine"


class CheckpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger_ratio: float = Field(default=0.5, gt=0, lt=1)
    derivations: DerivationConfig = Field(default_factory=DerivationConfig)


class HarnessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["opencode", "fake"] = "opencode"
    base_url: str = "http://127.0.0.1:4096"
    main_session_id: str | None = None
    model_context_tokens: int = Field(default=128000, gt=0)
    request_timeout_seconds: float = Field(default=120.0, gt=0)


class PCOConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_root: Path = Path(".pco")
    memory_dir: str = "memory"
    state_dir: str = "state"
    indexes_dir: str = "indexes"
    projection_dir: str = "projection"
    profile: str = "pco"
    checkpoint: CheckpointConfig = Field(default_factory=CheckpointConfig)
    harness: HarnessConfig = Field(default_factory=HarnessConfig)
    archive_reasoning: bool = True

    @property
    def memory_root(self) -> Path:
        return self.workspace_root / self.memory_dir

    @property
    def state_root(self) -> Path:
        return self.workspace_root / self.state_dir

    @property
    def indexes_root(self) -> Path:
        return self.workspace_root / self.indexes_dir

    @property
    def projection_root(self) -> Path:
        return self.workspace_root / self.projection_dir


def load_config(*, workspace: Path | None = None, overrides: list[str] | None = None) -> PCOConfig:
    config_dir = Path(str(files("pco.resources.config"))).resolve()
    effective_overrides = list(overrides or [])
    if workspace is not None:
        effective_overrides.append(f"workspace_root={workspace.resolve().as_posix()}")
    with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
        composed = compose(config_name="default", overrides=effective_overrides)
    if workspace is not None:
        local_path = workspace.resolve() / "config.yaml"
        if local_path.is_file():
            composed = OmegaConf.merge(composed, OmegaConf.load(local_path))
            composed.workspace_root = workspace.resolve().as_posix()
    raw = OmegaConf.to_container(composed, resolve=True)
    return PCOConfig.model_validate(raw)
