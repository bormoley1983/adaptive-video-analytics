"""CLI entry-point behavior checks (no GPU, no model download).

The CLI parses a single `ava run` subcommand and delegates to config loading
and the runner. These tests exercise argument parsing and the delegation
contract with controlled doubles so no real inference or I/O occurs.

The persistence preflight (`_verify_persistence`) is tested both in
isolation (unit) and through the CLI path (integration-style) to ensure
JSONL runs skip the PostgreSQL check and PostgreSQL runs perform it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ava import cli
from ava.config import PersistenceConfig


def _make_cfg(backend: str = "jsonl", dsn: str | None = None):
    """Build a minimal config object for CLI tests."""
    return type(
        "Cfg",
        (),
        {
            "gpu": "0",
            "cameras": [],
            "persistence": PersistenceConfig(backend=backend, dsn=dsn),
        },
    )()


def test_run_parses_config_and_delegates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_load_config(path: Path):
        cfg = _make_cfg(backend="jsonl")
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
        return _make_cfg(backend="jsonl")

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
        return _make_cfg(backend="jsonl")

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


def test_detection_only_cli_does_not_check_event_database(tmp_path, monkeypatch):
    config = _make_cfg(backend="postgres", dsn="postgresql://unused")
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "_verify_persistence", lambda config: pytest.fail("unexpected preflight"))
    monkeypatch.setattr(cli, "run", lambda config: type("Summary", (), {"to_dict": lambda self: {}})())
    assert cli.main(["run", "--config", str(tmp_path / "run.yaml")]) == 0


def test_camera_cli_checks_event_database(tmp_path, monkeypatch):
    config = _make_cfg(backend="postgres", dsn="postgresql://unused")
    config.cameras = [object()]
    checked = []
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "_verify_persistence", lambda cfg: checked.append(cfg))
    monkeypatch.setattr(cli, "run", lambda config: type("Summary", (), {"to_dict": lambda self: {}})())
    assert cli.main(["run", "--config", str(tmp_path / "run.yaml")]) == 0
    assert checked == [config]


# ------------------------------------------------------------------
# Persistence preflight tests (real _verify_persistence, no mock)
# ------------------------------------------------------------------


def test_verify_persistence_jsonl_skips_postgres() -> None:
    """JSONL backend skips the PostgreSQL preflight entirely."""
    config = _make_cfg(backend="jsonl")
    # Should return without raising — no PostgreSQL connection attempted.
    cli._verify_persistence(config)


def test_verify_persistence_postgres_builds_and_closes_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL backend builds the sink (connect + schema check) and closes it."""
    calls: list[str] = []

    class FakeSink:
        def __init__(self, dsn):
            calls.append(f"init:{dsn}")

        def check_schema(self):
            calls.append("check_schema")

        def close(self):
            calls.append("close")

    def fake_build(persistence, *, jsonl_path=None):
        return FakeSink(persistence.dsn)

    monkeypatch.setattr("ava.persistence.compose.build_event_sink", fake_build)
    config = _make_cfg(backend="postgres", dsn="postgresql://test@localhost/db")
    cli._verify_persistence(config)

    assert "init:postgresql://test@localhost/db" in calls
    assert "close" in calls


def test_verify_persistence_postgres_propagates_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed sink construction propagates the error."""
    import psycopg

    def failing_build(persistence, *, jsonl_path=None):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr("ava.persistence.compose.build_event_sink", failing_build)
    config = _make_cfg(backend="postgres", dsn="postgresql://test@localhost/db")
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        cli._verify_persistence(config)
