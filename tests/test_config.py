"""Deterministic config validation checks (no GPU, no model download)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ava.config import (
    DetectorConfig,
    LiveRunConfig,
    RunConfig,
    _resolve_env_refs,
    load_config,
    load_live_config,
)


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


class TestEnvResolution:
    """The main config handler resolves ${VAR:-default} at load time."""

    def test_resolve_env_refs_var_and_default(self, monkeypatch):
        monkeypatch.setenv("AVA_TEST_SET", "9500")
        monkeypatch.delenv("AVA_TEST_UNSET", raising=False)
        data = {
            "set": "${AVA_TEST_SET}",
            "with_default": "${AVA_TEST_UNSET:-9464}",
            "no_ref": "plain",
            "nested": {"inner": ["${AVA_TEST_SET}", "${AVA_TEST_UNSET:-7}"]},
        }
        out = _resolve_env_refs(data)
        assert out["set"] == "9500"
        assert out["with_default"] == "9464"
        assert out["no_ref"] == "plain"
        assert out["nested"]["inner"] == ["9500", "7"]

    def test_resolve_env_refs_empty_var_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AVA_TEST_EMPTY", "")
        assert _resolve_env_refs("${AVA_TEST_EMPTY:-fallback}") == "fallback"

    def test_load_live_config_resolves_prometheus_port(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AVA_METRICS_PORT", "9500")
        p = tmp_path / "live.yaml"
        p.write_text(
            "live:\n"
            "  backend: opencv\n"
            "  telemetry:\n"
            "    prometheus_port: ${AVA_METRICS_PORT:-9464}\n"
            "  cameras:\n"
            "    - id: cam-01\n"
            "      url: rtsp://127.0.0.1:8554/cam-01\n"
            "      profiles: []\n",
            encoding="utf-8",
        )
        cfg = load_live_config(p)
        assert cfg.telemetry.prometheus_port == 9500

    def test_load_live_config_prometheus_port_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AVA_METRICS_PORT", raising=False)
        p = tmp_path / "live.yaml"
        p.write_text(
            "live:\n"
            "  backend: opencv\n"
            "  telemetry:\n"
            "    prometheus_port: ${AVA_METRICS_PORT:-9464}\n"
            "  cameras:\n"
            "    - id: cam-01\n"
            "      url: rtsp://127.0.0.1:8554/cam-01\n"
            "      profiles: []\n",
            encoding="utf-8",
        )
        cfg = load_live_config(p)
        assert cfg.telemetry.prometheus_port == 9464

    def test_prometheus_host_defaults_to_loopback(self):
        cfg = LiveRunConfig(
            cameras=[
                {
                    "id": "cam-01",
                    "url": "rtsp://127.0.0.1:8554/cam-01",
                    "profiles": [],
                }
            ]
        )
        assert cfg.telemetry.prometheus_host == "127.0.0.1"
