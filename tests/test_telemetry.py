"""Unit tests for structured telemetry: JSON logs, redaction, bounded labels."""

from __future__ import annotations

import json

import pytest
from prometheus_client import REGISTRY

from ava.telemetry import (
    BoundedLabelsError,
    Telemetry,
    new_run_context,
    redact,
    redact_fields,
    render_metrics,
)


def _reset_registry() -> None:
    """Reset the module-level registry to a pristine state (test isolation)."""
    for collector in list(REGISTRY._collector_to_names):
        REGISTRY.unregister(collector)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test gets a fresh Prometheus registry (module-level singleton)."""
    _reset_registry()
    yield
    _reset_registry()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedact:
    def test_bearer_token_redacted(self):
        out = redact("header: Bearer abc123.def-456_789")
        assert "abc123" not in out
        assert "<redacted>" in out

    def test_password_key_value_redacted(self):
        out = redact("db password=hunter2 host=db.local")
        assert "hunter2" not in out
        assert "password=<redacted>" in out

    def test_api_key_colon_form_redacted(self):
        out = redact("api_key: sk-abcdef123456")
        assert "sk-abcdef123456" not in out

    def test_connection_string_credentials_redacted(self):
        out = redact("dsn=postgres://admin:s3cret@db.internal:5432/ava")
        assert "s3cret" not in out
        assert "admin" not in out
        assert "<redacted>@" in out

    def test_raw_rtsp_url_redacted(self):
        out = redact("source rtsp://10.0.0.5:8554/cam-01 failed")
        assert "rtsp://10.0.0.5" not in out
        assert "<rtsp-url-redacted>" in out

    def test_rtsps_url_redacted(self):
        out = redact("rtsps://user:pw@cam.example/live")
        assert "cam.example" not in out

    def test_clean_string_unchanged(self):
        out = redact("camera cam-01 decoded 100 frames")
        assert out == "camera cam-01 decoded 100 frames"

    def test_no_false_positive_on_bare_word_token(self):
        # The word 'token' alone without key=value shape is not a credential.
        out = redact("the token bucket algorithm works")
        assert out == "the token bucket algorithm works"


class TestRedactFields:
    def test_nested_structures_redacted(self):
        fields = {
            "url": "rtsp://10.0.0.5:8554/cam-01",
            "meta": {"dsn": "mysql://u:p@h/db", "note": "ok"},
            "items": ["Bearer xyz", "plain"],
        }
        out = redact_fields(fields)
        assert "<rtsp-url-redacted>" in out["url"]
        assert "p@" not in out["meta"]["dsn"]
        assert "<redacted>" in out["items"][0]
        assert out["items"][1] == "plain"

    def test_non_string_values_pass_through(self):
        out = redact_fields({"count": 3, "flag": True, "none": None})
        assert out == {"count": 3, "flag": True, "none": None}


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------


class TestRunContextLogging:
    def test_log_line_has_correlation_fields(self, capsys):
        ctx = new_run_context(camera_ids=["cam-01"])
        record = ctx.log(
            "live_run.start",
            "info",
            "starting",
            camera_id="cam-01",
            session_id="s1",
            event_id="cam-01/s1/t1/rule/kind-0",
            extra=42,
        )
        out = capsys.readouterr().out.strip()
        line = json.loads(out)
        assert line["run_id"] == record["run_id"]
        assert line["worker_id"] == ctx.worker_id
        assert line["camera_id"] == "cam-01"
        assert line["session_id"] == "s1"
        assert line["event_id"] == "cam-01/s1/t1/rule/kind-0"
        assert line["component"] == "live_run.start"
        assert line["severity"] == "info"
        assert line["schema_version"] == ctx.config_schema_version
        assert line["extra"] == 42
        # One JSON object per line.
        assert out.count("\n") == 0

    def test_optional_ids_omitted_when_unknown(self, capsys):
        ctx = new_run_context()
        ctx.log("live_run.end", "info", "done")
        line = json.loads(capsys.readouterr().out.strip())
        assert "camera_id" not in line
        assert "session_id" not in line
        assert "event_id" not in line

    def test_message_is_redacted(self, capsys):
        ctx = new_run_context()
        ctx.log("source.error", "error", "open failed rtsp://10.0.0.5:8554/cam-01")
        line = json.loads(capsys.readouterr().out.strip())
        assert "rtsp://" not in line["message"]
        assert "<rtsp-url-redacted>" in line["message"]

    def test_unknown_severity_degrades_to_info(self, capsys):
        ctx = new_run_context()
        record = ctx.log("x", "catastrophic", "msg")
        assert record["severity"] == "info"

    def test_unserializable_field_does_not_raise(self, capsys):
        ctx = new_run_context()

        class Bad:
            def __repr__(self) -> str:
                raise RuntimeError("nope")

        # default=str handles most objects; force a failure path via a value
        # that json cannot encode even with default (a function is encodable
        # via default=str, so use an object whose repr raises).
        record = ctx.log("x", "info", "msg", bad=Bad())
        assert record["severity"] == "info"

    def test_run_ids_are_unique(self):
        a = new_run_context()
        b = new_run_context()
        assert a.run_id != b.run_id


# ---------------------------------------------------------------------------
# Prometheus metrics with bounded labels
# ---------------------------------------------------------------------------


class TestTelemetryMetrics:
    def _telemetry(self) -> Telemetry:
        ctx = new_run_context(camera_ids=["cam-01", "cam-02"])
        return Telemetry(context=ctx, detector_mode="shared", backend="opencv")

    def test_record_decode_increments_counter(self):
        t = self._telemetry()
        t.record_decode("cam-01")
        t.record_decode("cam-01")
        text = render_metrics()
        assert 'ava_frames_decoded_total{camera="cam-01"' in text
        assert " 2.0" in text

    def test_record_inference_observes_histogram(self):
        t = self._telemetry()
        t.record_inference("cam-01", 0.05)
        text = render_metrics()
        assert 'ava_inference_duration_seconds_bucket{camera="cam-01"' in text
        # 0.05 falls in the le="0.05" bucket (and all wider ones).
        assert 'le="0.05"' in text

    def test_record_drop_rejects_unknown_reason(self):
        t = self._telemetry()
        with pytest.raises(BoundedLabelsError, match="drop reason"):
            t.record_drop("cam-01", "mystery")

    def test_record_drop_counts_bounded_reasons(self):
        t = self._telemetry()
        for reason in ("buffer_full", "reconnect", "error"):
            t.record_drop("cam-02", reason)
        text = render_metrics()
        assert 'reason="buffer_full"' in text
        assert 'reason="reconnect"' in text
        assert 'reason="error"' in text

    def test_camera_outside_inventory_rejected(self):
        t = self._telemetry()
        with pytest.raises(BoundedLabelsError, match="not in the configured inventory"):
            t.record_decode("cam-99")

    def test_empty_inventory_allows_any_camera(self):
        ctx = new_run_context(camera_ids=[])
        t = Telemetry(context=ctx)
        # No inventory constraint: nothing to bound against.
        t.record_decode("cam-any")
        assert "cam-any" in render_metrics()

    def test_record_error_rejects_unknown_kind(self):
        t = self._telemetry()
        with pytest.raises(BoundedLabelsError, match="error kind"):
            t.record_error("cam-01", "weird")

    def test_record_error_counts_bounded_kinds(self):
        t = self._telemetry()
        t.record_error("cam-01", "decode")
        text = render_metrics()
        assert 'kind="decode"' in text

    def test_event_latency_histogram_buckets_documented(self):
        t = self._telemetry()
        t.record_event("cam-01", 2.5)
        text = render_metrics()
        # The 3 s latency gate must be resolvable: a le="3.0" bucket exists.
        assert 'le="3.0"' in text
        assert "ava_event_latency_seconds_count" in text

    def test_queue_gauges_sampled(self):
        t = self._telemetry()
        t.sample_queue("cam-01", 7, 1.25)
        text = render_metrics()
        assert 'ava_queue_depth{camera="cam-01"' in text
        assert " 7.0" in text
        assert " 1.25" in text

    def test_mark_run_started_sets_gauge(self):
        t = self._telemetry()
        t.mark_run_started()
        text = render_metrics()
        assert f'run_id="{t.context.run_id}"' in text
        assert "ava_run_started" in text


# ---------------------------------------------------------------------------
# Scrape endpoint
# ---------------------------------------------------------------------------


class TestServeMetrics:
    def test_serves_metrics_payload(self):
        import socket
        import urllib.request

        from ava.telemetry import serve_metrics

        # Find a free loopback port.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        ctx = new_run_context(camera_ids=["cam-01"])
        telemetry = Telemetry(context=ctx)
        telemetry.record_decode("cam-01")

        thread = serve_metrics(port)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
                assert resp.status == 200
                body = resp.read().decode()
            assert "ava_frames_decoded_total" in body
        finally:
            thread.join(timeout=2)

    def test_unknown_path_returns_404(self):
        import socket
        import urllib.error
        import urllib.request

        from ava.telemetry import serve_metrics

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        thread = serve_metrics(port)
        try:
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
            assert excinfo.value.code == 404
        finally:
            thread.join(timeout=2)
