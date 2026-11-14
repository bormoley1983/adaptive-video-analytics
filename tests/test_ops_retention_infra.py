"""Prove age-based retention against isolated filesystem-backed services."""

from __future__ import annotations

import base64
import json
import os
import random
import string
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.infra
OPS_DIR = Path(__file__).resolve().parent.parent / "ops"


class _RetentionScenario:
    def __init__(self, scratch: Path, evidence: Path) -> None:
        self.scratch = scratch
        self.evidence = evidence
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.state = scratch / "state"
        self.state.mkdir()
        self.project = "ava-retention-" + uuid.uuid4().hex[:12]
        self.service = ""
        self.port = 0
        self.definition: dict = {}
        self.compose = [
            "docker", "compose", "--env-file", "/dev/null", "--project-name", self.project,
            "-f", str(scratch / "compose.yml"),
        ]

    def run(self, command: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        with (self.evidence / "commands.log").open("a") as log:
            log.write(f"command: {command!r}\nexit: {result.returncode}\n")
            log.write(f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\n")
        return result

    def checked(self, command: list[str], timeout: int = 180) -> str:
        result = self.run(command, timeout)
        assert result.returncode == 0, (
            f"{command!r} exited {result.returncode}: {result.stdout}\n{result.stderr}; "
            f"evidence: {self.evidence}"
        )
        return result.stdout

    def write_compose(self) -> None:
        (self.scratch / "compose.yml").write_text(yaml.safe_dump(self.definition, sort_keys=False))

    def configure(self, service: str, image: str, target: int, command: list[str]) -> None:
        self.service = service
        assert "@sha256:" in image, "retention service image must be digest-pinned"
        self.checked(["docker", "image", "inspect", image])
        self.definition = {
            "name": self.project,
            "services": {
                service: {
                    "image": image,
                    "pull_policy": "never",
                    "restart": "no",
                    "user": f"{os.getuid()}:{os.getgid()}",
                    "command": command,
                    "volumes": [
                        f"{self.scratch / 'config.yml'}:/etc/retention/config.yml:ro",
                        f"{self.state}:/state",
                    ],
                    "ports": [{"target": target, "published": "0", "host_ip": "127.0.0.1"}],
                    "mem_limit": "1g",
                    "stop_grace_period": "60s",
                }
            },
        }
        self.write_compose()

    def start(self) -> None:
        self.checked([*self.compose, "up", "-d", "--no-deps", self.service])
        target = 3100 if self.service == "loki" else 9090
        published = self.checked([*self.compose, "port", self.service, str(target)])
        self.port = int(published.strip().rsplit(":", 1)[1])
        path = "/ready" if self.service == "loki" else "/-/ready"
        self.wait(lambda: self.request(path) is not None, f"{self.service} readiness", 180)

    def stop(self) -> None:
        self.checked([*self.compose, "stop", "--timeout", "60", self.service])

    def request(self, path: str, payload: dict | None = None, method: str = "GET") -> bytes:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read()

    def wait(self, predicate: Callable[[], bool], description: str, timeout: int = 300) -> None:
        deadline = time.monotonic() + timeout
        last_error = "condition remained false"
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except (OSError, urllib.error.URLError, ValueError) as error:
                last_error = str(error)
            time.sleep(2)
        raise AssertionError(f"Timed out waiting for {description}: {last_error}; {self.evidence}")

    def inventory(self) -> dict[str, int]:
        return {
            str(path.relative_to(self.state)): path.stat().st_size
            for path in self.state.rglob("*") if path.is_file()
        }

    def bytes_used(self) -> int:
        return int(self.checked(["du", "-sb", str(self.state)]).split()[0])

    def record(self, name: str, payload: dict) -> None:
        (self.evidence / name).write_text(json.dumps(payload, indent=2, sort_keys=True))


@pytest.fixture
def retention_scenario(tmp_path: Path, request: pytest.FixtureRequest):
    evidence_root = Path(os.environ.get("AVA_TEST_EVIDENCE_DIR", "results/infra"))
    scenario = _RetentionScenario(tmp_path, evidence_root / request.node.name / uuid.uuid4().hex[:8])
    scenario.checked(["docker", "info"])
    try:
        yield scenario
    finally:
        if scenario.definition:
            scenario.run([*scenario.compose, "logs", "--no-color"])
            cleanup = scenario.run([*scenario.compose, "down", "--volumes", "--remove-orphans"])
            assert cleanup.returncode == 0, f"retention cleanup failed: {cleanup.stderr}"


def _loki_config() -> dict:
    config = yaml.safe_load((OPS_DIR / "loki/loki.yml").read_text())
    config["common"]["path_prefix"] = "/state"
    config["common"]["storage"]["filesystem"] = {
        "chunks_directory": "/state/chunks", "rules_directory": "/state/rules",
    }
    config["limits_config"].update({
        "retention_period": "24h",
        "reject_old_samples_max_age": "168h",
        "max_query_lookback": "0s",
    })
    config["compactor"].update({
        "working_directory": "/state/compactor",
        "retention_enabled": False,
        "compaction_interval": "2s",
        "retention_delete_delay": "2s",
        "retention_delete_worker_count": 1,
    })
    config["ingester"] = {
        "wal": {"enabled": False},
        "chunk_idle_period": "1s",
        "chunk_retain_period": "0s",
        "flush_check_period": "1s",
        "max_chunk_age": "1m",
        "lifecycler": {"min_ready_duration": "1s", "final_sleep": "0s"},
    }
    config["storage_config"] = {
        "tsdb_shipper": {
            "active_index_directory": "/state/tsdb-index",
            "cache_location": "/state/tsdb-cache",
            "cache_ttl": "1s",
            "resync_interval": "1s",
        }
    }
    config["ruler"]["storage"]["local"]["directory"] = "/state/rules"
    return config


def _loki_values(scenario: _RetentionScenario, generation: str, stamp: int) -> list:
    query = urllib.parse.urlencode({
        "query": f'{{retention_probe="{generation}"}}',
        "start": str(stamp - 1_000_000_000),
        "end": str(stamp + 2_000_000_000),
        "limit": "5000",
    })
    payload = json.loads(scenario.request(f"/loki/api/v1/query_range?{query}"))
    assert payload["status"] == "success", payload
    return [value for stream in payload["data"]["result"] for value in stream["values"]]


def _push_loki(scenario: _RetentionScenario, generation: str, stamp: int, count: int) -> None:
    rng = random.Random(generation)
    alphabet = string.ascii_letters + string.digits
    lines = [
        [str(stamp + index), f"{generation}-{index}-" + "".join(rng.choices(alphabet, k=1024))]
        for index in range(count)
    ]
    scenario.request("/loki/api/v1/push", {
        "streams": [{"stream": {"retention_probe": generation}, "values": lines}]
    }, "POST")
    scenario.request("/flush", method="POST")


def _chunk_paths(scenario: _RetentionScenario) -> set[str]:
    paths: set[str] = set()
    chunks = scenario.state / "chunks"
    for path in chunks.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(chunks)
        if relative.parts[0] == "fake":
            paths.add(str(path.relative_to(scenario.state)))
            continue
        try:
            key = base64.b64decode(path.name).decode()
        except (ValueError, UnicodeDecodeError):
            continue
        if key.startswith("fake/"):
            paths.add(str(path.relative_to(scenario.state)))
    return paths


def test_loki_retention_deletes_aged_chunks_and_bounds_state_volume(retention_scenario) -> None:
    scenario = retention_scenario
    config = _loki_config()
    config_path = scenario.scratch / "config.yml"
    config_path.write_text(yaml.safe_dump(config))
    shipped = yaml.safe_load((OPS_DIR / "docker-compose.yml").read_text())
    scenario.configure("loki", shipped["services"]["loki"]["image"], 3100,
                       ["-config.file=/etc/retention/config.yml"])
    scenario.start()
    current_stamp = int(time.time() * 1e9)
    old_stamp = current_stamp - 48 * 3600 * 1_000_000_000

    _push_loki(scenario, "current", current_stamp, 1024)
    scenario.wait(lambda: bool(_chunk_paths(scenario)), "current chunk flush")
    # A clean stop publishes the active TSDB index; query again from durable storage.
    scenario.stop()
    scenario.start()
    scenario.wait(lambda: len(_loki_values(scenario, "current", current_stamp)) == 1024,
                  "current data visible before retention")
    baseline = scenario.bytes_used()
    current_chunks = _chunk_paths(scenario)
    assert current_chunks, "no durable current chunk inventory"
    scenario.record("baseline.json", {"bytes": baseline, "files": scenario.inventory()})

    _push_loki(scenario, "old", old_stamp, 2048)
    scenario.wait(lambda: bool(_chunk_paths(scenario) - current_chunks), "aged chunk flush")
    scenario.stop()
    scenario.start()
    assert len(_loki_values(scenario, "old", old_stamp)) == 2048
    assert len(_loki_values(scenario, "current", current_stamp)) == 1024
    old_chunks = _chunk_paths(scenario) - current_chunks
    assert old_chunks, "aged data produced no distinguishable durable chunks"
    after_burst = scenario.bytes_used()
    assert after_burst > baseline, (baseline, after_burst)
    scenario.record("before-retention.json", {
        "bytes": after_burst, "old_stamp_ns": old_stamp, "current_stamp_ns": current_stamp,
        "old_chunks": sorted(old_chunks), "current_chunks": sorted(current_chunks),
        "old_lines": 2048, "current_lines": 1024, "files": scenario.inventory(),
    })

    scenario.stop()
    config["compactor"]["retention_enabled"] = True
    config_path.write_text(yaml.safe_dump(config))
    scenario.start()

    scenario.wait(
        lambda: all(not (scenario.state / path).exists() for path in old_chunks),
        "physical sweeping of every recorded aged chunk", 180,
    )
    scenario.record("physical-sweep.json", {
        "removed_old_chunks": sorted(old_chunks),
        "current_chunks": sorted(current_chunks),
        "files_after_sweep": scenario.inventory(),
    })
    assert all((scenario.state / path).exists() for path in current_chunks)
    # Embedded query caches can outlive swept objects. A cold reader proves
    # durable absence without waiting for the unrelated in-memory cache TTL.
    scenario.stop()
    scenario.start()

    def durable_deletion_visible() -> bool:
        old_absent = not _loki_values(scenario, "old", old_stamp)
        current_survives = len(_loki_values(scenario, "current", current_stamp)) == 1024
        return old_absent and current_survives

    scenario.wait(durable_deletion_visible, "cold query of retained data after physical sweeping", 60)
    scenario.wait(lambda: scenario.bytes_used() <= baseline * 1.2,
                  "post-retention state within 20% of baseline", 120)
    after_retention = scenario.bytes_used()
    scenario.record("retention-result.json", {
        "baseline_bytes": baseline, "post_burst_bytes": after_burst,
        "post_retention_bytes": after_retention, "maximum_bytes": baseline * 1.2,
        "removed_old_chunks": sorted(old_chunks), "current_chunks": sorted(current_chunks),
        "old_lines_after": len(_loki_values(scenario, "old", old_stamp)),
        "current_lines_after": len(_loki_values(scenario, "current", current_stamp)),
        "files_after": scenario.inventory(),
    })
    assert after_retention <= baseline * 1.2
    assert all((scenario.state / path).exists() for path in current_chunks)


def _prometheus_sample(scenario: _RetentionScenario, generation: str, stamp: int) -> list:
    query = urllib.parse.urlencode({
        "query": f'retention_probe{{generation="{generation}"}}', "time": stamp + 1,
    })
    payload = json.loads(scenario.request(f"/api/v1/query?{query}"))
    assert payload["status"] == "success", payload
    return payload["data"]["result"]


def test_prometheus_retention_removes_aged_blocks_and_preserves_current_data(
    retention_scenario,
) -> None:
    scenario = retention_scenario
    shipped = yaml.safe_load((OPS_DIR / "docker-compose.yml").read_text())
    image = shipped["services"]["prometheus"]["image"]
    assert "@sha256:" in image
    scenario.checked(["docker", "image", "inspect", image])
    now = int(time.time())
    old_stamp = now - 72 * 3600
    current_stamp = now - 3 * 3600
    source = scenario.scratch / "samples.prom"
    source.write_text(
        "# HELP retention_probe Synthetic age-based retention probe.\n"
        "# TYPE retention_probe gauge\n"
        f'retention_probe{{generation="old"}} 1 {old_stamp}\n'
        f'retention_probe{{generation="current"}} 2 {current_stamp}\n'
        "# EOF\n"
    )
    scenario.checked([
        "docker", "run", "--rm", "--pull", "never", "--network", "none",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{source}:/samples.prom:ro", "-v", f"{scenario.state}:/state",
        "--entrypoint", "/bin/promtool", image,
        "tsdb", "create-blocks-from", "openmetrics", "/samples.prom", "/state",
    ])
    blocks = {
        path.parent.name: json.loads(path.read_text())
        for path in scenario.state.glob("*/meta.json")
    }
    old_blocks = {name for name, meta in blocks.items() if meta["maxTime"] < current_stamp * 1000}
    current_blocks = set(blocks) - old_blocks
    assert old_blocks and current_blocks, f"expected distinct aged/current blocks: {blocks}"
    (scenario.scratch / "config.yml").write_text(yaml.safe_dump({
        "global": {"scrape_interval": "1s"}, "scrape_configs": [],
    }))
    command = [
        "--config.file=/etc/retention/config.yml", "--storage.tsdb.path=/state",
        "--storage.tsdb.retention.time=7d",
    ]
    scenario.configure("prometheus", image, 9090, command)
    scenario.start()
    assert _prometheus_sample(scenario, "old", old_stamp)
    assert _prometheus_sample(scenario, "current", current_stamp)
    scenario.record("before-retention.json", {
        "old_stamp": old_stamp, "current_stamp": current_stamp, "blocks": blocks,
        "old_blocks": sorted(old_blocks), "current_blocks": sorted(current_blocks),
        "files": scenario.inventory(),
    })

    scenario.stop()
    scenario.definition["services"]["prometheus"]["command"][-1] = (
        "--storage.tsdb.retention.time=24h"
    )
    scenario.write_compose()
    scenario.start()

    def aged_blocks_removed() -> bool:
        gone = all(not (scenario.state / block).exists() for block in old_blocks)
        old_absent = not _prometheus_sample(scenario, "old", old_stamp)
        current_survives = bool(_prometheus_sample(scenario, "current", current_stamp))
        return gone and old_absent and current_survives

    scenario.wait(aged_blocks_removed, "aged Prometheus blocks deleted by retention", 180)
    scenario.record("retention-result.json", {
        "removed_old_blocks": sorted(old_blocks), "surviving_current_blocks": sorted(current_blocks),
        "old_query_after": _prometheus_sample(scenario, "old", old_stamp),
        "current_query_after": _prometheus_sample(scenario, "current", current_stamp),
        "files_after": scenario.inventory(),
    })
    assert all((scenario.state / block).exists() for block in current_blocks)
