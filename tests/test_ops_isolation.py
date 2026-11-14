"""Failure cleanup and isolation contracts without starting services."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import test_ops_infra as infra
import yaml


def test_partial_startup_failure_cleans_only_disposable_project(tmp_path_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(infra, "_docker_available", lambda: True)
    monkeypatch.setenv("AVA_TEST_EVIDENCE_DIR", str(tmp_path / "evidence"))
    def render(command, **kwargs):
        assert command[-3:] == ["config", "--format", "json"]
        assert kwargs["env"]["AVA_ALERT_WEBHOOK_URL"] == infra.WEBHOOK_URL
        config = {"services": {
            "postgres": {"environment": {"POSTGRES_PASSWORD": "test-only-postgres"},
                         "ports": [{"target": 5432}]},
            "node-exporter": {"volumes": []},
        }, "volumes": {"ops-postgres": {}}, "networks": {"default": {}}}
        return subprocess.CompletedProcess(command, 0, json.dumps(config), "")
    monkeypatch.setattr(infra.subprocess, "run", render)
    observed = {}
    def compose(*args):
        project = infra.COMPOSE[infra.COMPOSE.index("--project-name") + 1]
        assert project.startswith("ava-infra-")
        assert project != "ava-ops"
        observed["project"] = project
        config_path = Path(infra.COMPOSE[-1])
        config = yaml.safe_load(config_path.read_text())
        assert config["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"] == "test-only-postgres"
        assert "${" not in config_path.read_text()
        for service in config["services"].values():
            for port in service.get("ports", []):
                assert port["host_ip"] == "127.0.0.1" and port["published"] == "0"
        assert "/dev/null" in infra.COMPOSE
        if args[0] == "up":
            return subprocess.CompletedProcess(args, 1, "", "injected startup failure")
        if args[0] == "down":
            observed["cleaned"] = project
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(infra, "_compose", compose)
    fixture = infra.ops_stack.__wrapped__(tmp_path_factory)
    with pytest.raises(AssertionError, match="injected startup failure"):
        next(fixture)
    assert observed["cleaned"] == observed["project"]
    assert infra.COMPOSE == []
