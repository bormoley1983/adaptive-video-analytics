"""CLI entry-point behavior checks (no GPU, no model download).

The CLI parses a single `ava run` subcommand and delegates to config loading
and the runner. These tests exercise argument parsing and the delegation
contract with controlled doubles so no real inference or I/O occurs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ava import cli


def test_run_parses_config_and_delegates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_load_config(path: Path):
        cfg = type("Cfg", (), {"gpu": "0"})()
        seen["config_path"] = path
        return cfg

    def fake_run(config):
        seen["config"] = config
        seen["gpu"] = config.gpu
        summary = type("Summary", (), {"to_dict": lambda self: {"frames": 3}})()
        return summary

    monkeypatch.setattr(cli, "load_config", fake_load_config)
    monkeypatch.setattr(cli, "run", fake_run)

    cfg_file = tmp_path / "run.yaml"
    cfg_file.write_text("source: /videos/input.mp4\n", encoding="utf-8")

    code = cli.main(["run", "--config", str(cfg_file)])

    assert code == 0
    assert seen["config_path"] == cfg_file
    assert seen["gpu"] == "0"


def test_run_gpu_override_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_load_config(path: Path):
        return type("Cfg", (), {"gpu": "0"})()

    def fake_run(config):
        seen["gpu"] = config.gpu
        return type("Summary", (), {"to_dict": lambda self: {}})()

    monkeypatch.setattr(cli, "load_config", fake_load_config)
    monkeypatch.setattr(cli, "run", fake_run)

    cfg_file = tmp_path / "run.yaml"
    cfg_file.write_text("source: /videos/input.mp4\n", encoding="utf-8")

    code = cli.main(["run", "--config", str(cfg_file), "--gpu", "1"])

    assert code == 0
    assert seen["gpu"] == "1"


def test_run_prints_json_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture) -> None:
    def fake_load_config(path: Path):
        return type("Cfg", (), {"gpu": "0"})()

    def fake_run(config):
        return type("Summary", (), {"to_dict": lambda self: {"frames": 7, "events": 2}})()

    monkeypatch.setattr(cli, "load_config", fake_load_config)
    monkeypatch.setattr(cli, "run", fake_run)

    cfg_file = tmp_path / "run.yaml"
    cfg_file.write_text("source: /videos/input.mp4\n", encoding="utf-8")

    code = cli.main(["run", "--config", str(cfg_file)])

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["frames"] == 7
    assert out["events"] == 2


def test_missing_config_argument_rejected(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    # argparse exits with code 2 when a required argument is missing.
    with pytest.raises(SystemExit) as exc:
        cli.main(["run"])
    assert exc.value.code == 2
