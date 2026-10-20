"""Run configuration for AVA.

Configuration is data, not code: a single run is described by a YAML file so
that E-2 can change behaviour without editing Python. For E-1 the fields cover
one input clip, one detector path and output locations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class DetectorConfig(BaseModel):
    """Which inference path to run and with what settings."""

    kind: Literal["conventional", "prompted"] = "conventional"
    # Checkpoint file (e.g. models/yolo26n.pt or models/yoloe-26s-seg.pt).
    model: Path
    # For the prompted path: text class prompts. Ignored for conventional.
    prompts: list[str] = Field(default_factory=list)
    # Text encoder asset for YOLOE (e.g. models/mobileclip2_b.ts).
    text_encoder: Path | None = None
    confidence: float = 0.35
    iou: float = 0.5


class RunConfig(BaseModel):
    """A single AVA run over one input clip."""

    source: Path
    detector: DetectorConfig
    output_dir: Path = Path("results")
    # Explicit GPU selection (index or UUID). None => let the runtime decide.
    gpu: str | None = "0"
    # Persist per-frame detection records as JSONL.
    jsonl: bool = True
    # Write an annotated MP4 alongside the JSONL.
    annotate: bool = True


def load_config(path: Path) -> RunConfig:
    """Load and validate a run configuration from YAML."""
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return RunConfig.model_validate(raw)
