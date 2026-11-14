"""Exercise disposable infrastructure without controlling shared services."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
import yaml

OPS_DIR = Path(__file__).resolve().parent.parent / "ops"
COMPOSE: list[str] = []
CONFIG_DIR = OPS_DIR
EVIDENCE_DIR: Path | None = None
POSTGRES_PORT = 0
GRAFANA_PORT = 3000
PROMETHEUS_PORT = 9090
LOKI_PORT = 3100
NGINX_IMAGE = "nginx@sha256:713fc2ebfd92e8217db451477182ee91944b123d2b1ee9fd5a8c6e91ca6c3a11"
WEBHOOK_URL = "http://webhook-sink:8080/alerts"


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.infra


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    if not COMPOSE:
        raise RuntimeError("isolated Compose fixture is not active")
    result = subprocess.run(
        [*COMPOSE, *args], check=False, capture_output=True, text=True, timeout=600
    )
    if EVIDENCE_DIR is not None:
        with (EVIDENCE_DIR / "compose.log").open("a") as log:
            log.write(f"command: {' '.join(args)}\n{result.stdout}\n{result.stderr}\n")
    return result


def _http_get(url: str, timeout: float = 10.0, headers: dict[str, str] | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _wait_healthy(service: str, timeout_s: int = 180) -> None:
    """Wait until one replica of the service reports healthy."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        proc = _compose("ps", "--format", "json")
        for line in proc.stdout.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("Service") == service and (entry.get("Health") == "healthy"):
                return
        time.sleep(5)
    raise AssertionError(f"service {service!r} did not become healthy in {timeout_s}s")


def _tcp_connect(ip: str, port: int, timeout: float = 3.0) -> bool:
    """True when a TCP connection to ip:port succeeds within timeout."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _port_bound_to_loopback_only(port: int) -> bool:
    """True when the port answers on 127.0.0.1 and not on the host LAN IP."""
    if not _tcp_connect("127.0.0.1", port):
        return False
    # A non-loopback bind would also answer on the host's primary address.
    hostname_ip = socket.gethostbyname(socket.gethostname())
    if hostname_ip.startswith("127."):
        return True
    # A non-loopback bind would also answer on the host's primary address.
    return not _tcp_connect(hostname_ip, port)


def _refresh_ports():
    global POSTGRES_PORT, GRAFANA_PORT, PROMETHEUS_PORT, LOKI_PORT

    def port(service, target):
        result = _compose("port", service, str(target))
        assert result.returncode == 0, result.stderr
        return int(result.stdout.strip().rsplit(":", 1)[1])

    POSTGRES_PORT = port("postgres", 5432)
    GRAFANA_PORT = port("grafana", 3000)
    PROMETHEUS_PORT = port("prometheus", 9090)
    LOKI_PORT = port("loki", 3100)


@pytest.fixture(scope="module")
def ops_stack(tmp_path_factory):
    """Own a disposable project, including configuration and database state."""
    global COMPOSE, CONFIG_DIR, EVIDENCE_DIR
    if not _docker_available():
        pytest.skip("Docker runtime unavailable")
    project = "ava-infra-" + uuid.uuid4().hex[:12]
    scratch = tmp_path_factory.mktemp(project)
    EVIDENCE_DIR = Path(os.environ.get("AVA_TEST_EVIDENCE_DIR", "results/infra")).resolve() / project
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR = scratch
    for directory in ("prometheus", "loki", "alloy", "grafana", "alertmanager"):
        shutil.copytree(OPS_DIR / directory, scratch / directory)
    backups = scratch / "custom-backups"
    backups.mkdir()
    _write_backup_marker(backups)
    alloy = scratch / "alloy/config.alloy"
    alloy.write_text(
        alloy.read_text().replace(
            'discovery.docker "linux" {',
            'discovery.docker "linux" {\n  filter {\n    name = "label"\n'
            f'    values = ["com.docker.compose.project={project}"]\n  }}',
        )
    )
    render_env = {
        **os.environ,
        "POSTGRES_PASSWORD": "test-only-postgres",
        "GRAFANA_ADMIN_USER": "admin",
        "GRAFANA_ADMIN_PASSWORD": "test-only-grafana",
        "AVA_ALERT_WEBHOOK_URL": WEBHOOK_URL,
        "AVA_BACKUP_DIR": str(backups),
    }
    rendered = subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "-f",
         str(OPS_DIR / "docker-compose.yml"), "config", "--format", "json"],
        env=render_env, capture_output=True, text=True, check=False, timeout=30,
    )
    assert rendered.returncode == 0, rendered.stderr
    config = json.loads(rendered.stdout)
    config["name"] = project
    for section in ("volumes", "networks", "configs"):
        for name, resource in config.get(section, {}).items():
            resource["name"] = f"{project}_{name}"
    for service in config["services"].values():
        service["restart"] = "no"
        service["pull_policy"] = "never"
        service["ports"] = [
            {"target": port["target"], "published": "0", "host_ip": "127.0.0.1"}
            for port in service.get("ports", [])
        ]
        for mount in service.get("volumes", []):
            if mount["type"] == "bind":
                source = Path(mount["source"])
                if source.is_relative_to(OPS_DIR):
                    mount["source"] = str(scratch / source.relative_to(OPS_DIR))
    node = config["services"]["node-exporter"]
    node["command"] = ["--collector.textfile.directory=/textfile", "--no-collector.filesystem"]
    node["volumes"] = [m for m in node["volumes"] if m["target"] != "/host"]
    prom_path = scratch / "prometheus/prometheus.yml"
    prom_config = yaml.safe_load(prom_path.read_text())
    prom_config["global"].update(scrape_interval="2s", evaluation_interval="2s")
    prom_path.write_text(yaml.safe_dump(prom_config, sort_keys=False))
    am_path = scratch / "alertmanager/alertmanager.yml"
    am_config = yaml.safe_load(am_path.read_text())
    am_config["route"].update(group_wait="0s", group_interval="1s")
    for route in am_config["route"].get("routes", []):
        route.update(group_wait="0s", group_interval="1s")
    am_path.write_text(yaml.safe_dump(am_config, sort_keys=False))
    sink_config = scratch / "nginx.conf"
    sink_config.write_text("""events {}
http {
 log_format alerts escape=json '{"body":"$request_body"}';
 server { listen 8080; access_log /var/log/nginx/alerts.log alerts;
  location /alerts { proxy_pass http://127.0.0.1:8081; } }
 server { listen 127.0.0.1:8081; access_log off; return 204; }
}
""")
    config["services"]["webhook-sink"] = {
        "image": NGINX_IMAGE,
        "pull_policy": "never",
        "volumes": [f"{sink_config}:/etc/nginx/nginx.conf:ro"],
        "healthcheck": {
            "test": ["CMD", "wget", "-qO-", "http://127.0.0.1:8081"],
            "interval": "2s", "timeout": "2s", "retries": 15,
        },
        "mem_limit": "128m",
    }
    compose_file = scratch / "compose.yml"
    compose_file.write_text(yaml.safe_dump(config, sort_keys=False))
    COMPOSE = ["docker", "compose", "--env-file", "/dev/null", "--project-name", project,
               "-f", str(compose_file)]
    try:
        checked = _compose("config", "--quiet")
        assert checked.returncode == 0, checked.stderr
        up = _compose("up", "-d")
        assert up.returncode == 0, up.stderr
        for service in config["services"]:
            _wait_healthy(service)
        _refresh_ports()
        yield project
    finally:
        try:
            _compose("logs", "--no-color")
            sink_log = _compose("exec", "-T", "webhook-sink", "cat", "/var/log/nginx/alerts.log")
            (EVIDENCE_DIR / "webhook-receipts.jsonl").write_text(sink_log.stdout)
        finally:
            cleanup = _compose("down", "--volumes", "--remove-orphans")
            COMPOSE = []
            assert cleanup.returncode == 0, cleanup.stderr


def test_postgres_is_disposable_and_reachable(ops_stack):
    with psycopg.connect(
        host="127.0.0.1",
        port=POSTGRES_PORT,
        dbname="ava",
        user="ava",
        password="test-only-postgres",
    ) as conn:
        conn.execute("CREATE TABLE infra_probe (value integer)")
        conn.execute("INSERT INTO infra_probe VALUES (42)")
        assert conn.execute("SELECT value FROM infra_probe").fetchone()[0] == 42
    assert _port_bound_to_loopback_only(POSTGRES_PORT)


def test_all_services_healthy(ops_stack: str) -> None:
    proc = _compose("ps", "--format", "json")
    entries = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    services = {e.get("Service", "") for e in entries}
    assert "prometheus" in services
    assert "grafana" in services
    assert "loki" in services
    assert "alloy" in services
    assert "postgres" in services
    assert "postgres-exporter" in services
    assert "node-exporter" in services
    assert "alertmanager" in services
    for entry in entries:
        if entry.get("Health"):
            assert entry["Health"] == "healthy", f"{entry['Name']} not healthy"


def test_ports_bound_to_loopback_only(ops_stack: str) -> None:
    assert _port_bound_to_loopback_only(GRAFANA_PORT)
    assert _port_bound_to_loopback_only(PROMETHEUS_PORT)
    assert _port_bound_to_loopback_only(LOKI_PORT)


def test_prometheus_serves_and_scrapes_itself(ops_stack: str) -> None:
    # The first self-scrape lands ~15s after start; wait for it.
    deadline = time.monotonic() + 60
    found = False
    while time.monotonic() < deadline and not found:
        body = _http_get(f"http://127.0.0.1:{PROMETHEUS_PORT}/api/v1/query?query=up")
        payload = json.loads(body)
        assert payload["status"] == "success"
        for result in payload["data"]["result"]:
            metric = result["metric"]
            value = float(result["value"][1])
            if metric.get("job") == "prometheus" and value == 1.0:
                found = True
                break
        if not found:
            time.sleep(5)
    assert found, "prometheus self-scrape (up==1) was not observed within 60s"


def test_ava_target_scrape(ops_stack: str) -> None:
    """A stub /metrics endpoint on the host is discovered by Prometheus."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    port = 0
    payload = b"# HELP ava_frames_decoded_total Frames decoded per camera.\n"
    payload += b"# TYPE ava_frames_decoded_total counter\n"
    payload += b'ava_frames_decoded_total{service="ava",worker="stub",camera="cam-01"} 42\n'

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            return

    # Bind to 0.0.0.0: Prometheus reaches the host via host-gateway
    # (172.17.0.1), not loopback, so a loopback-only bind is unreachable.
    server = HTTPServer(("0.0.0.0", port), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Point the ava job at the stub and reload Prometheus.
        prom_cfg = CONFIG_DIR / "prometheus" / "prometheus.yml"
        original = prom_cfg.read_text()
        patched = original.replace("host.docker.internal:9464", f"host.docker.internal:{port}")
        prom_cfg.write_text(patched)
        try:
            reload_req = urllib.request.Request(
                f"http://127.0.0.1:{PROMETHEUS_PORT}/-/reload", method="POST"
            )
            with urllib.request.urlopen(reload_req, timeout=10) as reload:
                assert reload.status == 200
            # Wait for the scrape to land (scrape interval is 15s).
            deadline = time.monotonic() + 60
            found = False
            while time.monotonic() < deadline and not found:
                body = _http_get(
                    f"http://127.0.0.1:{PROMETHEUS_PORT}"
                    "/api/v1/query?query=ava_frames_decoded_total"
                )
                data = json.loads(body)
                found = any(r["metric"].get("camera") == "cam-01" for r in data["data"]["result"])
                if not found:
                    time.sleep(5)
            assert found, "stub AVA target was not scraped into Prometheus"
        finally:
            prom_cfg.write_text(original)
            restore_req = urllib.request.Request(
                f"http://127.0.0.1:{PROMETHEUS_PORT}/-/reload", method="POST"
            )
            with urllib.request.urlopen(restore_req, timeout=10):
                pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_loki_receives_structured_logs(ops_stack: str) -> None:
    """A JSON log line pushed through the Loki API is queryable."""
    import uuid

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    record = {
        "ts": "2026-10-23T12:00:00.000Z",
        "severity": "info",
        "event": "live_run.start",
        "component": "live_runner",
        "message": "infra test log line",
        "run_id": run_id,
        "camera_id": "cam-01",
    }
    line = json.dumps(record)
    ts_ns = str(int(time.time() * 1e9))
    push = {
        "streams": [
            {
                "stream": {"job": "ava", "service": "ava", "container": "infra-test"},
                "values": [[ts_ns, line]],
            }
        ]
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{LOKI_PORT}/loki/api/v1/push",
        data=json.dumps(push).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 204

    deadline = time.monotonic() + 30
    found = False
    while time.monotonic() < deadline and not found:
        query = urllib.parse.quote(f'{{job="ava"}} |~ "{run_id}"')
        body = _http_get(
            f"http://127.0.0.1:{LOKI_PORT}/loki/api/v1/query_range?query={query}&limit=10"
        )
        data = json.loads(body)
        for stream in data.get("data", {}).get("result", []):
            for _ts, value in stream.get("values", []):
                if run_id in value:
                    found = True
                    break
        if not found:
            time.sleep(3)
    assert found, "structured log line was not queryable in Loki"


def test_backend_outage_does_not_block_scrape_api(ops_stack: str) -> None:
    """Stopping Loki and Grafana leaves Prometheus serving queries."""
    stop = _compose("stop", "loki", "grafana")
    assert stop.returncode == 0, f"compose stop failed: {stop.stderr}"
    try:
        body = _http_get(f"http://127.0.0.1:{PROMETHEUS_PORT}/api/v1/query?query=up", timeout=15)
        payload = json.loads(body)
        assert payload["status"] == "success"
    finally:
        start = _compose("start", "loki", "grafana")
        assert start.returncode == 0, f"compose start failed: {start.stderr}"
        _wait_healthy("loki")
        _wait_healthy("grafana")
        _refresh_ports()


def test_state_persists_across_restart(ops_stack: str) -> None:
    """Prometheus TSDB and Loki chunks survive a full stack restart."""
    # Seed a recognizable series via the stub-target scrape already performed,
    # then restart the whole project and check the data is still there.
    restart = _compose("restart")
    assert restart.returncode == 0, f"compose restart failed: {restart.stderr}"
    for service in ("postgres", "postgres-exporter", "prometheus", "loki", "alloy", "grafana", "node-exporter", "alertmanager"):
        _wait_healthy(service)
    _refresh_ports()
    with psycopg.connect(
        host="127.0.0.1",
        port=POSTGRES_PORT,
        dbname="ava",
        user="ava",
        password="test-only-postgres",
    ) as conn:
        assert conn.execute("SELECT value FROM infra_probe").fetchone()[0] == 42
    body = _http_get(f"http://127.0.0.1:{PROMETHEUS_PORT}/api/v1/query?query=up")
    payload = json.loads(body)
    assert payload["status"] == "success"
    # The prometheus self-scrape series must still be present after restart.
    assert any(r["metric"].get("job") == "prometheus" for r in payload["data"]["result"])


# --- alert rules and retention bounds --------------------------------


def _reload_prometheus() -> None:
    """Trigger a Prometheus config reload via the lifecycle endpoint."""
    req = urllib.request.Request(f"http://127.0.0.1:{PROMETHEUS_PORT}/-/reload", method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200


def _prometheus_query(query: str) -> list[dict]:
    """Run a Prometheus instant query and return the result list."""
    body = _http_get(
        f"http://127.0.0.1:{PROMETHEUS_PORT}/api/v1/query?query={urllib.parse.quote(query)}"
    )
    payload = json.loads(body)
    assert payload["status"] == "success", payload
    return payload["data"]["result"]


def test_alert_rules_loaded(ops_stack: str) -> None:
    """Gate 1: all five B-024-S1 alert rules are loaded by Prometheus."""
    body = _http_get(f"http://127.0.0.1:{PROMETHEUS_PORT}/api/v1/rules")
    payload = json.loads(body)
    assert payload["status"] == "success", payload
    rule_names = {r["name"] for group in payload["data"]["groups"] for r in group.get("rules", [])}
    expected = {
        "AvaServiceDown",
        "PrometheusConfigReloadFailed",
        "PrometheusTSDBCompactionsFailing",
        "LokiRequestErrorsHigh",
        "PostgresUnavailable",
    }
    missing = expected - rule_names
    assert not missing, f"expected alert rules not loaded: {missing}"


def _prometheus_rule_state(rule_name: str) -> str | None:
    """Return the rule-level state for a named alert rule, or None if absent."""
    body = _http_get(f"http://127.0.0.1:{PROMETHEUS_PORT}/api/v1/rules")
    payload = json.loads(body)
    assert payload["status"] == "success", payload
    for group in payload["data"]["groups"]:
        for r in group.get("rules", []):
            if r["name"] == rule_name:
                return r.get("state")
    return None


def test_alert_fire_and_recovery(ops_stack: str) -> None:
    """Gate 2: simulated AVA target down triggers AvaServiceDown; recovery clears it.

    Uses the config-reload path (F6): point the ava job at an unreachable port,
    reload, wait for the alert to fire, restore to a reachable in-network target
    (Loki), reload, wait for the alert to stop firing. The test-only rules.yml
    uses a short `for` duration (F5) so the full cycle fits within the suite
    budget.

    Note: we cannot use a host stub server here because host.docker.internal
    routing from inside the compose network to arbitrary host ports is not
    guaranteed in all Docker configurations. Loki (port 3100, always up in the
    test stack) serves as a reliable reachable target for the recovery phase.
    """
    # Ensure Prometheus is fully ready after any prior test's restart.
    # The previous test (test_state_persists_across_restart) does a full compose
    # restart; give Prometheus one scrape interval to re-establish targets.
    _wait_healthy("prometheus")
    time.sleep(20)

    # Rewrite the scratch rules.yml to use a 15s `for` on AvaServiceDown so the
    # fire/resolve cycle completes in ~60-90s instead of ~4-6 min.
    rules_path = CONFIG_DIR / "prometheus" / "rules.yml"
    original_rules = rules_path.read_text()
    patched_rules = original_rules.replace("for: 2m", "for: 15s")
    rules_path.write_text(patched_rules)

    # Point the ava job at an unreachable port.
    prom_cfg = CONFIG_DIR / "prometheus" / "prometheus.yml"
    original_cfg = prom_cfg.read_text()
    patched_cfg = original_cfg.replace("host.docker.internal:9464", "host.docker.internal:1")
    prom_cfg.write_text(patched_cfg)

    try:
        _reload_prometheus()

        # Wait for up{job="ava"} to drop to 0 (one scrape interval, 15s).
        deadline = time.monotonic() + 60
        up_down = False
        while time.monotonic() < deadline and not up_down:
            results = _prometheus_query('up{job="ava"}')
            if results and float(results[0]["value"][1]) == 0.0:
                up_down = True
            else:
                time.sleep(5)
        assert up_down, 'up{job="ava"} did not drop to 0 within 60s'

        # Wait for AvaServiceDown rule to reach firing state (for: 15s + eval interval).
        deadline = time.monotonic() + 90
        fired = False
        while time.monotonic() < deadline and not fired:
            if _prometheus_rule_state("AvaServiceDown") == "firing":
                fired = True
            else:
                time.sleep(5)
        assert fired, "AvaServiceDown rule did not reach firing state within 90s"

        # Restore config pointing at Loki (always up in the test stack) and reload.
        restore_cfg = original_cfg.replace("host.docker.internal:9464", "loki:3100")
        prom_cfg.write_text(restore_cfg)
        _reload_prometheus()

        # Wait for up{job="ava"} to recover to 1.
        deadline = time.monotonic() + 60
        up_recovered = False
        while time.monotonic() < deadline and not up_recovered:
            results = _prometheus_query('up{job="ava"}')
            if results and float(results[0]["value"][1]) == 1.0:
                up_recovered = True
            else:
                time.sleep(5)
        assert up_recovered, 'up{job="ava"} did not recover to 1 within 60s'

        # Require the loaded rule to be inactive; a missing or pending rule
        # does not demonstrate recovery.
        deadline = time.monotonic() + 90
        cleared = False
        while time.monotonic() < deadline and not cleared:
            if _prometheus_rule_state("AvaServiceDown") == "inactive":
                cleared = True
            else:
                time.sleep(5)
        assert cleared, "AvaServiceDown rule did not become inactive within 90s"
    finally:
        prom_cfg.write_text(original_cfg)
        rules_path.write_text(original_rules)
        _reload_prometheus()


def _wait_postgres_metrics(expected_up: int, expected_pg_up: int | None) -> None:
    deadline = time.monotonic() + 90
    observed = {}
    while time.monotonic() < deadline:
        for name in ("up", "pg_up"):
            observed[name] = [
                float(result["value"][1])
                for result in _prometheus_query(f'{name}{{job="postgres"}}')
            ]
        if observed["up"] == [expected_up] and (
            expected_pg_up is None or observed["pg_up"] == [expected_pg_up]
        ):
            return
        time.sleep(3)
    raise AssertionError(
        f"expected postgres up={expected_up}, pg_up={expected_pg_up}; got {observed}"
    )


def _wait_rule_state(rule_name: str, expected: str) -> None:
    deadline = time.monotonic() + 120
    observed = None
    while time.monotonic() < deadline:
        observed = _prometheus_rule_state(rule_name)
        if observed == expected:
            return
        time.sleep(3)
    raise AssertionError(f"expected {rule_name} state={expected}; got {observed}")


@pytest.mark.parametrize("service", ["postgres", "postgres-exporter"])
def test_postgres_alert_fire_and_recovery(ops_stack: str, service: str) -> None:
    """Database failure alerts even while the exporter remains scrapeable."""
    rules_path = CONFIG_DIR / "prometheus" / "rules.yml"
    original_rules = rules_path.read_text()
    rules = yaml.safe_load(original_rules)
    rule = next(
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule["alert"] == "PostgresUnavailable"
    )
    assert rule["for"] == "2m"
    assert rule["labels"]["severity"] == "critical"
    # Only the disposable copy uses a shorter alert delay.
    rule["for"] = "15s"
    try:
        rules_path.write_text(yaml.safe_dump(rules, sort_keys=False))
        _reload_prometheus()
        _wait_postgres_metrics(expected_up=1, expected_pg_up=1)
        _wait_rule_state("PostgresUnavailable", "inactive")
        try:
            stopped = _compose("stop", service)
            assert stopped.returncode == 0, stopped.stderr
            if service == "postgres":
                _wait_postgres_metrics(expected_up=1, expected_pg_up=0)
            else:
                _wait_postgres_metrics(expected_up=0, expected_pg_up=None)
            _wait_rule_state("PostgresUnavailable", "firing")
        finally:
            started = _compose("start", service)
            assert started.returncode == 0, started.stderr
            _wait_healthy(service)
            _refresh_ports()
        _wait_postgres_metrics(expected_up=1, expected_pg_up=1)
        _wait_rule_state("PostgresUnavailable", "inactive")
    finally:
        rules_path.write_text(original_rules)
        _reload_prometheus()


def test_loki_rejects_old_samples(ops_stack: str) -> None:
    """Gate 4 (Loki): a log sample with a timestamp older than 168h is rejected.

    Verified behavior on the pinned Loki 3.7.9 image (F4): HTTP 400 with
    'timestamp too old' in the body.
    """
    # Push a log line with a timestamp 8 days in the past.
    ts_8d_ago_ns = str(int((time.time() - 8 * 24 * 3600) * 1e9))
    push = {
        "streams": [
            {
                "stream": {"job": "retention-test", "service_name": "test"},
                "values": [[ts_8d_ago_ns, "retention probe line"]],
            }
        ]
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{LOKI_PORT}/loki/api/v1/push",
        data=json.dumps(push).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=10)
    assert exc_info.value.code == 400, (
        f"expected HTTP 400 for old sample, got {exc_info.value.code}"
    )
    body = exc_info.value.read().decode()
    assert "too old" in body, f"expected 'too old' in rejection body, got: {body[:200]}"


def test_prometheus_retention_flag_present(ops_stack: str) -> None:
    """Gate 3/4 (Prometheus): the 7-day retention flag is present in the shipped config.

    Static check on the rendered compose file (QA answer 5: no short-retention
    enforcement probe). Also verifies the postgres-exporter target is visible in
    Prometheus targets.
    """
    # Static check: the retention flag must be in the ops compose file.
    compose_text = (OPS_DIR / "docker-compose.yml").read_text()
    assert "--storage.tsdb.retention.time=7d" in compose_text, (
        "Prometheus 7-day retention flag not found in ops/docker-compose.yml"
    )

    # Dynamic check: the postgres-exporter target is registered and up.
    deadline = time.monotonic() + 60
    pg_up = False
    while time.monotonic() < deadline and not pg_up:
        results = _prometheus_query('up{job="postgres"}')
        if results and float(results[0]["value"][1]) == 1.0:
            pg_up = True
        else:
            time.sleep(5)
    assert pg_up, "postgres-exporter target (job=postgres) not up within 60s"


def _pg_connect(dbname: str = "ava"):
    return psycopg.connect(
        host="127.0.0.1",
        port=POSTGRES_PORT,
        dbname=dbname,
        user="ava",
        password="test-only-postgres",
    )


def _project_network(project: str) -> str:
    # The fixture renders a scratch compose file without the top-level `name:`
    # key, so the default network is named after --project-name.
    return f"{project}_default"


def test_backup_restore_drill_proves_consistency_and_records_rto(ops_stack: str) -> None:
    """Backup, destroy, restore, then prove row-level consistency and record RTO.

    Seeds representative events/outbox rows (including evidence metadata),
    takes a logical dump, drops and recreates the database, restores from the
    dump, re-applies migrations, and verifies counts, FK integrity and
    evidence-column integrity. The measured recovery time is written to the
    per-run evidence directory as an RTO data point (no hard bound asserted;
    thresholds are provisional under B-003/B-004/B-009).
    """
    assert EVIDENCE_DIR is not None
    backup_dir = EVIDENCE_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "AVA_PG_HOST": "127.0.0.1",
        "AVA_PG_PORT": str(POSTGRES_PORT),
        "AVA_PG_USER": "ava",
        "AVA_PG_PASSWORD": "test-only-postgres",
        "AVA_PG_DB": "ava",
        "AVA_BACKUP_DIR": str(backup_dir),
        "AVA_TEST_EVIDENCE_DIR": str(EVIDENCE_DIR),
        "AVA_PG_DSN": f"postgresql://ava:test-only-postgres@127.0.0.1:{POSTGRES_PORT}/ava",
    }

    def run_backup(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(OPS_DIR / "backup.sh"), *args],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
        )

    # Arrange: apply migrations and seed representative rows.
    with _pg_connect() as conn:
        conn.execute("SELECT 1")
    alembic = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert alembic.returncode == 0, alembic.stderr

    with _pg_connect() as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO events (event_id, camera_id, session_id, track_id, rule_id, rule_type, class_label, confidence, source_time_s, emitted_utc, time_basis, config_version, schema_version, frame_index, evidence_kind, evidence_path, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), 'file_pts_only', 'cfg-1', 'v1', %s, %s, %s, %s)",
                (f"evt-{i}", f"cam-{i % 2}", "sess-1", i, "rule-1", "zone_entry", "person", 0.9, float(i), i, "clip" if i % 2 == 0 else None, f"/evidence/clip-{i}.mp4" if i % 2 == 0 else None, "{}"),
            )
        for i in range(5):
            conn.execute(
                "INSERT INTO outbox (event_id, aggregate_type, payload) VALUES (%s, 'event', %s)",
                (f"evt-{i}", "{}"),
            )
        conn.commit()

    # Act 1: backup. The host psql wrapper is version-agnostic; run the dump
    # through the pinned postgres image so client and server versions match.
    dump_file = (backup_dir / f"ava-{uuid.uuid4().hex[:8]}.dump").resolve()
    dump = subprocess.run(
        [*COMPOSE, "run", "--rm", "-e", "PGPASSWORD=test-only-postgres",
         "--entrypoint", "pg_dump", "postgres",
         "-h", "postgres", "-p", "5432", "-U", "ava", "-d", "ava", "-Fc"],
        check=False,
        capture_output=True,
        timeout=600,
    )
    assert dump.returncode == 0, dump.stderr.decode()
    with open(dump_file, "wb") as fh:
        fh.write(dump.stdout)
    assert dump_file.stat().st_size > 0, "empty dump"

    # Act 2: restore drill (drop + recreate + restore + migrations).
    rto_start = time.monotonic()
    admin = _pg_connect(dbname="postgres")
    admin.autocommit = True
    try:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'ava' AND pid <> pg_backend_pid()"
        )
        admin.execute("DROP DATABASE ava WITH (FORCE)")
        admin.execute("CREATE DATABASE ava OWNER ava")
    finally:
        admin.close()

    # pg_restore in the pinned postgres:18 image does not honor "-" as stdin;
    # bind-mount the dump file into the container and pass the path instead.
    restore = subprocess.run(
        [
            "docker", "run", "--rm",
            "-e", "PGPASSWORD=test-only-postgres",
            "--entrypoint", "pg_restore",
            "--network", _project_network(ops_stack),
            "--mount", f"type=bind,source={dump_file},target=/tmp/restore.dump,readonly",
            "postgres:18@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6",
            "-h", "postgres", "-p", "5432", "-U", "ava", "-d", "ava",
            "--no-owner", "/tmp/restore.dump",
        ],
        check=False,
        capture_output=True,
    )
    if restore.returncode != 0:
        # Record diagnostics for review; the compose project and its network
        # are still up at this point.
        diag = subprocess.run(
            ["docker", "network", "ls", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        (EVIDENCE_DIR / "restore-diag.txt").write_text(
            f"project={ops_stack}\nnetworks:\n{diag.stdout}\nstderr:\n{restore.stderr.decode()}"
        )
    assert restore.returncode == 0, restore.stderr.decode()

    alembic_restore = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert alembic_restore.returncode == 0, alembic_restore.stderr

    rto_s = round(time.monotonic() - rto_start, 2)

    # Record the RTO data point as an evidence artifact (no hard bound asserted;
    # thresholds are provisional under B-003/B-004/B-009).
    with _pg_connect() as conn:
        ev_rows = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        ob_rows = conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
    rto_file = EVIDENCE_DIR / f"rto-{uuid.uuid4().hex[:8]}.json"
    rto_file.write_text(
        json.dumps(
            {"rto_seconds": rto_s, "dump": str(dump_file), "events_rows": ev_rows, "outbox_rows": ob_rows},
            indent=2,
        )
    )

    # Assert: row-level consistency after restore.
    with _pg_connect() as conn:
        events_rows = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        outbox_rows = conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
        orphan_outbox = conn.execute(
            "SELECT count(*) FROM outbox o LEFT JOIN events e ON e.event_id = o.event_id WHERE e.event_id IS NULL"
        ).fetchone()[0]
        evidence_events = conn.execute(
            "SELECT count(*) FROM events WHERE evidence_path IS NOT NULL"
        ).fetchone()[0]
    assert events_rows == 5, f"expected 5 events after restore, got {events_rows}"
    assert outbox_rows == 5, f"expected 5 outbox rows after restore, got {outbox_rows}"
    assert orphan_outbox == 0, f"{orphan_outbox} outbox rows reference missing events"
    assert evidence_events == 3, f"expected 3 events with evidence_path, got {evidence_events}"

    # Assert: RTO data point recorded in the per-run evidence directory.
    rto_files = sorted(EVIDENCE_DIR.glob("rto-*.json"))
    assert rto_files, "no RTO evidence artifact written"
    rto = json.loads(rto_files[-1].read_text())
    assert rto["rto_seconds"] > 0
    assert rto["events_rows"] == 5
    assert rto["outbox_rows"] == 5


def _write_backup_marker(directory: Path, age_seconds: int = 0, epoch: int | None = None) -> Path:
    marker = directory / "last-backup-epoch.prom"
    temporary = directory / "backup-marker.tmp"
    temporary.write_text(f"ava_backup_last_success_epoch {epoch or int(time.time())}\n")
    stamp = time.time() - age_seconds
    os.utime(temporary, (stamp, stamp))
    temporary.replace(marker)
    return marker


def _wait_query_value(query: str, expected: float, timeout_s: int = 60) -> None:
    deadline = time.monotonic() + timeout_s
    observed = []
    while time.monotonic() < deadline:
        observed = [float(result["value"][1]) for result in _prometheus_query(query)]
        if observed == [expected]:
            return
        time.sleep(1)
    raise AssertionError(f"expected {query}={expected}; observed {observed}")


def _wait_alert(rule_name: str, state: str, labels: dict[str, str], timeout_s: int = 90) -> None:
    deadline = time.monotonic() + timeout_s
    observed = []
    inactive_since = None
    while time.monotonic() < deadline:
        payload = json.loads(_http_get(f"http://127.0.0.1:{PROMETHEUS_PORT}/api/v1/rules"))
        rules = [rule for group in payload["data"]["groups"] for rule in group["rules"]
                 if rule.get("name") == rule_name]
        assert rules, f"loaded rule {rule_name} missing"
        if any(rule["health"] == "unknown" for rule in rules):
            time.sleep(1)
            continue
        assert all(rule["health"] == "ok" for rule in rules), rules
        observed = [alert for rule in rules for alert in rule.get("alerts", [])
                    if all(alert["labels"].get(key) == value for key, value in labels.items())]
        if state == "inactive" and not observed:
            inactive_since = inactive_since or time.monotonic()
            if time.monotonic() - inactive_since >= 6:
                return
        else:
            inactive_since = None
        if state != "inactive" and any(alert["state"] == state for alert in observed):
            return
        time.sleep(1)
    raise AssertionError(f"expected {rule_name}{labels} {state}; observed {observed}")


@contextmanager
def _short_alert_delays(*names: str, extra_labels: dict[str, str] | None = None):
    path = CONFIG_DIR / "prometheus/rules.yml"
    original = path.read_text()
    rules = yaml.safe_load(original)
    selected = [rule for group in rules["groups"] for rule in group["rules"]
                if rule.get("alert") in names]
    assert set(names) <= {rule["alert"] for rule in selected}
    for rule in selected:
        rule["for"] = "2s"
        rule.setdefault("labels", {}).update(extra_labels or {})
    try:
        path.write_text(yaml.safe_dump(rules, sort_keys=False))
        _reload_prometheus()
        yield
    finally:
        path.write_text(original)
        _reload_prometheus()


def _webhook_receipt(token: str, status: str, timeout_s: int = 90) -> dict:
    deadline = time.monotonic() + timeout_s
    observed = []
    while time.monotonic() < deadline:
        log = _compose("exec", "-T", "webhook-sink", "cat", "/var/log/nginx/alerts.log")
        assert log.returncode == 0, log.stderr
        observed = []
        for line in log.stdout.splitlines():
            envelope = json.loads(line)
            body = json.loads(envelope["body"])
            observed.append(body)
            for alert in body.get("alerts", []):
                if alert["labels"].get("notification_test") == token and alert["status"] == status:
                    return body
        time.sleep(1)
    raise AssertionError(f"no {status} webhook for {token}; observed {observed}")


def test_new_alert_rules_loaded(ops_stack: str) -> None:
    payload = json.loads(_http_get(f"http://127.0.0.1:{PROMETHEUS_PORT}/api/v1/rules"))
    rules = [rule for group in payload["data"]["groups"] for rule in group["rules"]]
    names = {rule["name"] for rule in rules}
    assert {"AvaOpsVolumeFillHigh", "AvaBackupStale", "AvaOpsServiceDown"} <= names
    severities = {rule["labels"]["severity"] for rule in rules if rule["name"] == "AvaOpsVolumeFillHigh"}
    assert severities == {"warning", "critical"}


@pytest.mark.parametrize("service", ["grafana", "alloy"])
def test_ops_outage_fires_recovers_and_delivers_webhook(ops_stack: str, service: str) -> None:
    token = uuid.uuid4().hex
    selector = {"job": service, "notification_test": token}
    with _short_alert_delays("AvaOpsServiceDown", extra_labels={"notification_test": token}):
        for healthy in ("grafana", "alloy"):
            _wait_query_value(f'up{{job="{healthy}"}}', 1)
        _wait_alert("AvaOpsServiceDown", "inactive", selector)
        try:
            stopped = _compose("stop", service)
            assert stopped.returncode == 0, stopped.stderr
            _wait_query_value(f'up{{job="{service}"}}', 0)
            _wait_alert("AvaOpsServiceDown", "firing", selector)
            receipt = _webhook_receipt(token, "firing")
            assert any(alert["labels"].get("job") == service for alert in receipt["alerts"])
        finally:
            started = _compose("start", service)
            assert started.returncode == 0, started.stderr
            _wait_healthy(service)
            _refresh_ports()
        _wait_query_value(f'up{{job="{service}"}}', 1)
        _wait_alert("AvaOpsServiceDown", "inactive", selector)
        _webhook_receipt(token, "resolved")


@pytest.mark.parametrize("used_percent,severity,fstype", [
    (85, None, "ext4"), (86, "warning", "ext4"), (95, "warning", "ext4"),
    (96, "critical", "ext4"), (99, None, "tmpfs"), (99, None, "overlay"),
])
def test_volume_fill_thresholds_fire_and_recover(
    ops_stack: str, used_percent: int, severity: str | None, fstype: str,
) -> None:
    path = CONFIG_DIR / "custom-backups/filesystem.prom"

    def write_usage(used: int) -> None:
        temporary = path.with_suffix(".tmp")
        labels = f'device="fixture",fstype="{fstype}",mountpoint="/fixture"'
        temporary.write_text(
            f"node_filesystem_size_bytes{{{labels}}} 100\n"
            f"node_filesystem_avail_bytes{{{labels}}} {100 - used}\n"
        )
        temporary.replace(path)
        _wait_query_value('node_filesystem_avail_bytes{device="fixture"}', 100 - used)

    with _short_alert_delays("AvaOpsVolumeFillHigh"):
        try:
            write_usage(used_percent)
            for level in ("warning", "critical"):
                _wait_alert("AvaOpsVolumeFillHigh", "firing" if severity == level else "inactive",
                            {"device": "fixture", "severity": level})
        finally:
            write_usage(50)
        _wait_alert("AvaOpsVolumeFillHigh", "inactive", {"device": "fixture"})
        path.unlink()


@pytest.mark.parametrize("condition", ["stale", "missing"])
def test_backup_age_or_missing_marker_fires_and_recovers(ops_stack: str, condition: str) -> None:
    backups = CONFIG_DIR / "custom-backups"
    selector = {"job": "node"}
    with _short_alert_delays("AvaBackupStale"):
        _write_backup_marker(backups, age_seconds=86300, epoch=1)
        _wait_query_value('time() - node_textfile_mtime_seconds{job="node",file="/textfile/last-backup-epoch.prom"} < bool 86400', 1)
        _wait_alert("AvaBackupStale", "inactive", selector)
        try:
            if condition == "stale":
                _write_backup_marker(backups, age_seconds=86500)
            else:
                (backups / "last-backup-epoch.prom").unlink()
            _wait_alert("AvaBackupStale", "firing", selector)
        finally:
            _write_backup_marker(backups, epoch=1)
        _wait_alert("AvaBackupStale", "inactive", selector)


def test_notification_resources_are_loaded(ops_stack: str) -> None:
    auth = base64.b64encode(b"admin:test-only-grafana").decode()
    headers = {"Authorization": f"Basic {auth}"}
    root = f"http://127.0.0.1:{GRAFANA_PORT}"
    contact_points = json.loads(_http_get(f"{root}/api/v1/provisioning/contact-points", headers=headers))
    contact = next(point for point in contact_points if point["uid"] == "ava-webhook")
    assert contact["type"] == "webhook"
    assert contact["settings"]["url"] == WEBHOOK_URL
    policy = json.loads(_http_get(f"{root}/api/v1/provisioning/policies", headers=headers))
    assert policy["receiver"] == contact["name"]
    assert any(route["receiver"] == contact["name"] for route in policy["routes"])
    datasources = json.loads(_http_get(f"{root}/api/datasources", headers=headers))
    alertmanager = next(source for source in datasources if source["type"] == "alertmanager")
    assert alertmanager["url"] == "http://alertmanager:9093"
    assert alertmanager["jsonData"]["implementation"] == "prometheus"
    loaded = _compose("exec", "-T", "alertmanager", "wget", "-qO-",
                      "http://127.0.0.1:9093/api/v2/status")
    assert loaded.returncode == 0, loaded.stderr
    status = json.loads(loaded.stdout)
    config = yaml.safe_load(status["config"]["original"])
    assert config["route"]["receiver"] == "ava-webhook"
    receiver = next(receiver for receiver in config["receivers"] if receiver["name"] == "ava-webhook")
    assert receiver["webhook_configs"][0]["url_file"] == "/etc/alertmanager-webhook-url"


def test_webhook_url_is_required() -> None:
    env = {**os.environ, "POSTGRES_PASSWORD": "test-only-postgres",
           "GRAFANA_ADMIN_PASSWORD": "test-only-grafana"}
    env.pop("AVA_ALERT_WEBHOOK_URL", None)
    rendered = subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "-f",
         str(OPS_DIR / "docker-compose.yml"), "config", "--quiet"],
        env=env, capture_output=True, text=True, check=False, timeout=30,
    )
    assert rendered.returncode != 0
    assert "AVA_ALERT_WEBHOOK_URL" in rendered.stderr


def test_backup_directory_override_updates_the_collected_success_marker(ops_stack: str) -> None:
    backups = CONFIG_DIR / "custom-backups"
    binaries = CONFIG_DIR / "backup-bin"
    binaries.mkdir()
    wrapper = binaries / "pg_dump"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "if os.environ.get('AVA_TEST_DUMP_FAIL'):\n    sys.exit(7)\n"
        "target = Path(sys.argv[sys.argv.index('-f') + 1])\n"
        f"command = {COMPOSE!r} + ['exec', '-T', '-e', 'PGPASSWORD=test-only-postgres', "
        "'postgres', 'pg_dump', '-U', 'ava', '-d', 'ava', '-Fc']\n"
        "with target.open('wb') as output:\n"
        "    result = subprocess.run(command, stdout=output, check=False)\n"
        "sys.exit(result.returncode)\n"
    )
    wrapper.chmod(0o755)
    env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}",
           "AVA_PG_PASSWORD": "test-only-postgres", "AVA_BACKUP_DIR": str(backups)}
    marker = _write_backup_marker(backups, age_seconds=86500)
    previous_mtime = marker.stat().st_mtime_ns
    failed = subprocess.run(
        ["bash", str(OPS_DIR / "backup.sh"), "backup"], env={**env, "AVA_TEST_DUMP_FAIL": "1"},
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert failed.returncode == 7, failed.stderr
    assert marker.stat().st_mtime_ns == previous_mtime

    successful = subprocess.run(
        ["bash", str(OPS_DIR / "backup.sh"), "backup"], env=env,
        capture_output=True, text=True, check=False, timeout=120,
    )
    assert EVIDENCE_DIR is not None
    (EVIDENCE_DIR / "backup-script.log").write_text(successful.stdout + successful.stderr)
    assert successful.returncode == 0, successful.stderr
    assert any(dump.stat().st_size > 0 for dump in backups.glob("ava-*.dump"))
    assert marker.stat().st_mtime_ns > previous_mtime
    _wait_query_value('time() - node_textfile_mtime_seconds{job="node",file="/textfile/last-backup-epoch.prom"} < bool 60', 1)
    assert not list(backups.glob(".last-backup-epoch.*"))


def test_backup_relative_directory_override_is_rejected(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(OPS_DIR / "backup.sh"), "backup"], cwd=tmp_path,
        env={**os.environ, "AVA_PG_PASSWORD": "test-only-postgres", "AVA_BACKUP_DIR": "relative-backups"},
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert result.returncode != 0
    assert "absolute" in result.stdout + result.stderr
    assert not (tmp_path / "relative-backups").exists()
