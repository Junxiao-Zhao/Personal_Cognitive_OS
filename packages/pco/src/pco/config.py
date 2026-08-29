from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Literal

from hydra import compose, initialize_config_dir
from mem_core.errors import MemError
from omegaconf import OmegaConf
from pydantic import BaseModel, ConfigDict, Field


LEGACY_TRIGGER_RATIO_FIELD = "checkpoint.trigger_ratio"
LEGACY_TRIGGER_RATIO_NOTICE = {
    "code": "CONFIG_MIGRATION_REQUIRED",
    "kind": "migration_error",
    "machine_detectable": True,
    "field": LEGACY_TRIGGER_RATIO_FIELD,
    "replacement": ["checkpoint.auto_consolidate.new_public_tokens", "checkpoint.auto_compact.context_ratio"],
    "action": "Remove checkpoint.trigger_ratio and set both v0.4.0 thresholds explicitly.",
    "next": "Run /consolidate before migrating the configuration.",
}


class DerivationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: bool = True
    backlinks: bool = True
    projection: Literal["affine", "markdown", "none"] = "affine"


class AutoConsolidateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    new_public_tokens: int = Field(default=32768, gt=0)


class AutoCompactConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    context_ratio: float = Field(default=0.90, gt=0, lt=1)


class CheckpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_consolidate: AutoConsolidateConfig = Field(default_factory=AutoConsolidateConfig)
    auto_compact: AutoCompactConfig = Field(default_factory=AutoCompactConfig)
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
    for override in effective_overrides:
        if override.split("=", 1)[0].strip() == LEGACY_TRIGGER_RATIO_FIELD:
            raise _legacy_trigger_ratio_error(override)
    if workspace is not None:
        effective_overrides.append(f"workspace_root={workspace.resolve().as_posix()}")
    with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
        composed = compose(config_name="default", overrides=effective_overrides)
    if workspace is not None:
        local_path = workspace.resolve() / "config.yaml"
        if local_path.is_file():
            local = OmegaConf.load(local_path)
            local_raw = OmegaConf.to_container(local, resolve=False)
            if _has_legacy_trigger_ratio(local_raw):
                raise _legacy_trigger_ratio_error(local_path)
            composed = OmegaConf.merge(composed, local)
            composed.workspace_root = workspace.resolve().as_posix()
    raw = OmegaConf.to_container(composed, resolve=True)
    return PCOConfig.model_validate(raw)


def _has_legacy_trigger_ratio(value: object) -> bool:
    return isinstance(value, dict) and isinstance(value.get("checkpoint"), dict) and "trigger_ratio" in value["checkpoint"]


def _legacy_trigger_ratio_error(source: object) -> MemError:
    notice = {
        **LEGACY_TRIGGER_RATIO_NOTICE,
        "source": str(source),
    }
    return MemError(
        "CONFIG_MIGRATION_REQUIRED",
        "config",
        "Legacy checkpoint.trigger_ratio is not accepted by PCO v0.4.0; migrate both checkpoint thresholds explicitly.",
        path=str(source),
        value=notice,
        recovery=[
            "Run /consolidate before migrating the configuration",
            "Remove checkpoint.trigger_ratio from the workspace config",
            "Set checkpoint.auto_consolidate.new_public_tokens",
            "Set checkpoint.auto_compact.context_ratio",
        ],
    )
