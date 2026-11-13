"""Exercise disposable infrastructure without controlling shared services."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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


def _http_get(url: str, timeout: float = 10.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
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
    EVIDENCE_DIR = Path(os.environ.get("AVA_TEST_EVIDENCE_DIR", "results/infra")) / project
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR = scratch
    for directory in ("prometheus", "loki", "alloy", "grafana"):
        shutil.copytree(OPS_DIR / directory, scratch / directory)
    # Restrict log discovery to this project instead of reading shared workloads.
    alloy = scratch / "alloy/config.alloy"
    alloy.write_text(
        alloy.read_text().replace(
            'discovery.docker "linux" {',
            'discovery.docker "linux" {\n  filter {\n    name = "label"\n'
            f'    values = ["com.docker.compose.project={project}"]\n  }}',
        )
    )
    text = (OPS_DIR / "docker-compose.yml").read_text()
    text = text.replace(
        "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in ops/.env}", "test-only-postgres"
    )
    text = text.replace(
        "${GRAFANA_ADMIN_PASSWORD:?set GRAFANA_ADMIN_PASSWORD in ops/.env}", "test-only-grafana"
    )
    text = text.replace("${GRAFANA_ADMIN_USER:-admin}", "admin")
    config = yaml.safe_load(text)
    config["name"] = project
    for service in config["services"].values():
        service["restart"] = "no"
        service["ports"] = [
            {"target": int(port.rsplit(":", 1)[1]), "published": "0", "host_ip": "127.0.0.1"}
            for port in service.get("ports", [])
        ]
        # Rewrite relative volume mounts to point at the scratch copies so the
        # test can patch config files without touching the shipped ops/ tree.
        if "volumes" in service:
            service["volumes"] = [
                str(scratch / vol.split(":", 1)[0].lstrip("./"))
                + ":"
                + ":".join(vol.split(":")[1:])
                if vol.startswith("./") and len(vol.split(":")) >= 2
                else vol
                for vol in service["volumes"]
            ]
    compose_file = scratch / "compose.yml"
    compose_file.write_text(yaml.safe_dump(config, sort_keys=False))
    COMPOSE = [
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "--project-name",
        project,
        "-f",
        str(compose_file),
    ]
    try:
        rendered = _compose("config", "--quiet")
        assert rendered.returncode == 0, rendered.stderr
        up = _compose("up", "-d")
        assert up.returncode == 0, up.stderr
        for service in ("postgres", "postgres-exporter", "prometheus", "loki", "alloy", "grafana"):
            _wait_healthy(service)
        _refresh_ports()
        yield project
    finally:
        try:
            _compose("logs", "--no-color")
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
    for service in ("postgres", "postgres-exporter", "prometheus", "loki", "alloy", "grafana"):
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
    deadline = time.monotonic() + 90
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
