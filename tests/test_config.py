"""Deterministic config validation checks (no GPU, no model download)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ava.config import DetectorConfig, RunConfig, load_config


def test_conventional_config_roundtrip(tmp_path: Path) -> None:
    cfg = RunConfig(
        source=tmp_path / "input.mp4",
        detector=DetectorConfig(kind="conventional", model=tmp_path / "yolo26n.pt"),
    )
    assert cfg.detector.kind == "conventional"
    assert cfg.gpu == "0"


def test_prompted_requires_prompts_field() -> None:
    det = DetectorConfig(
        kind="prompted",
        model=Path("m.pt"),
        prompts=["person", "car"],
    )
    assert det.prompts == ["person", "car"]


def test_load_config_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "run.yaml"
    p.write_text(
        "source: /videos/input.mp4\n"
        "detector:\n"
        "  kind: conventional\n"
        "  model: /models/yolo26n.pt\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.source == Path("/videos/input.mp4")
    assert cfg.detector.model == Path("/models/yolo26n.pt")


def test_invalid_kind_rejected(tmp_path: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DetectorConfig(kind="bogus", model=tmp_path / "m.pt")  # type: ignore[arg-type]
