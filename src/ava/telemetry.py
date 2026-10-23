"""Structured telemetry for live runs (B-021).

Two narrow concerns, kept separate from inference and rule code:

- Structured JSON logs on stdout. One object per line with a fixed set of
  correlation fields (run_id, camera_id, session_id, worker_id, event_id)
  plus component, severity, message and schema version. Secrets and raw
  source URLs are redacted before emission; log collection is an external
  concern (Alloy/Loki), so this module never imports a collector client and
  a missing collector cannot block or crash a run.
- A Prometheus scrape surface built on ``prometheus_client`` with bounded
  labels only: service, worker, camera from the configured finite inventory,
  detector mode, backend and drop/error category. Event, track and request
  IDs are log fields, never labels. Histogram buckets are documented at
  definition time and aligned to the latency gates in the E-3 completion
  criteria.

The scrape endpoint is optional (off unless a port is configured) so runtime
progress never depends on a telemetry backend.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Bearer tokens and Authorization headers in any casing.
_BEARER_RE = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"
)
# Generic credential-looking key=value / key: value pairs.
_CREDENTIAL_KEY_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|authorization)\b(\s*[:=]\s*)\S+"
)
# Connection strings with embedded credentials (scheme://user:pass@host).
_CONN_STR_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*)://[^:/\s]+:[^@\s]+@"
)
# Raw RTSP/RTMPS source URLs.
_RTSP_URL_RE = re.compile(r"(?i)\brtsps?://[^\s\"']+")


def redact(value: str) -> str:
    """Remove secrets and raw source URLs from a string before logging."""
    value = _BEARER_RE.sub(r"\1<redacted>", value)
    value = _CREDENTIAL_KEY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", value)
    value = _CONN_STR_RE.sub(r"\1://<redacted>@", value)
    value = _RTSP_URL_RE.sub("<rtsp-url-redacted>", value)
    return value


def redact_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact string values in a structured field mapping."""
    out: dict[str, Any] = {}
    for key, val in fields.items():
        out[key] = _redact_value(val)
    return out


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

_SEVERITIES = ("debug", "info", "warning", "error")


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class RunContext:
    """Correlation identity for one live run.

    ``run_id`` is unique per process invocation; ``worker_id`` identifies the
    analytics worker (one per process in the current single-process runtime).
    Camera, session and event identities are attached per log record only when
    known, so a single context serves every camera thread of the run.
    """

    run_id: str
    worker_id: str = "ava-live-1"
    config_schema_version: str = "1"
    environment: str = "local"
    # Finite camera inventory; used to bound Prometheus label values.
    camera_ids: tuple[str, ...] = field(default_factory=tuple)

    def log(
        self,
        component: str,
        severity: str,
        message: str,
        *,
        camera_id: str | None = None,
        session_id: str | None = None,
        event_id: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Emit one structured JSON log line to stdout.

        Returns the record that was emitted (useful for tests). Never raises
        on redaction or serialization problems: telemetry must not break a
        run, so a malformed field degrades to a sanitized string.
        """
        if severity not in _SEVERITIES:
            severity = "info"
        record: dict[str, Any] = {
            "ts": _utc_now_iso(),
            "severity": severity,
            "event": component,
            "component": component,
            "message": redact(message),
            "schema_version": self.config_schema_version,
            "environment": self.environment,
            "run_id": self.run_id,
            "worker_id": self.worker_id,
        }
        if camera_id is not None:
            record["camera_id"] = camera_id
        if session_id is not None:
            record["session_id"] = session_id
        if event_id is not None:
            record["event_id"] = event_id
        record.update(redact_fields(fields))
        try:
            line = json.dumps(record, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001 - telemetry must never break a run
            # A field whose repr/str raises (or any other serialization
            # failure) degrades to the core correlation fields only.
            safe = {
                k: v
                for k, v in record.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
            line = json.dumps(safe, sort_keys=True)
        print(line, file=sys.stdout, flush=True)
        return record


def new_run_context(
    camera_ids: list[str] | tuple[str, ...] | None = None,
    config_schema_version: str = "1",
    environment: str = "local",
) -> RunContext:
    """Create a fresh run context with a unique run id."""
    import uuid

    return RunContext(
        run_id=f"run-{uuid.uuid4().hex[:12]}",
        camera_ids=tuple(camera_ids or ()),
        config_schema_version=config_schema_version,
        environment=environment,
    )


# ---------------------------------------------------------------------------
# Prometheus scrape surface
# ---------------------------------------------------------------------------

# Documented histogram buckets (seconds). Aligned to the E-3 completion gate:
# primary-event p95 latency < 3 s, so buckets resolve well below and above it.
EVENT_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0)
INFERENCE_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)

# Bounded label value sets. Camera comes from the configured finite inventory;
# everything else is a fixed category list. Event/track/request IDs are never
# labels (unbounded population).
DROP_REASONS = ("buffer_full", "reconnect", "error")
ERROR_KINDS = ("decode", "inference", "rules", "sink", "other")


class BoundedLabelsError(ValueError):
    """Raised when a label value falls outside the bounded inventory."""


@dataclass
class Telemetry:
    """Prometheus metrics for one live run, with bounded labels.

    All metric names are prefixed ``ava_``. Metrics register into the
    process-wide default REGISTRY; constructing a second Telemetry in the same
    process (e.g. across tests) requires resetting the registry first (see
    ``tests/test_telemetry.py::_reset_registry``).
    """

    context: RunContext
    detector_mode: str = "shared"
    backend: str = "opencv"
    _queue_gauges: dict[str, Any] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        from prometheus_client import REGISTRY, Counter, Gauge, Histogram

        # A second Telemetry in the same process (tests, repeated runs) would
        # otherwise hit DuplicateTimeseries; reset to a pristine registry.
        for collector in list(REGISTRY._collector_to_names):
            REGISTRY.unregister(collector)

        base_labels = ("service", "worker", "camera")
        self.frames_decoded_total = Counter(
            "ava_frames_decoded_total",
            "Frames decoded per camera.",
            base_labels,
        )
        self.frames_inferred_total = Counter(
            "ava_frames_inferred_total",
            "Frames passed to inference per camera.",
            base_labels,
        )
        self.frames_dropped_total = Counter(
            "ava_frames_dropped_total",
            "Frames dropped by reason per camera.",
            base_labels + ("reason",),
        )
        self.events_emitted_total = Counter(
            "ava_events_emitted_total",
            "Rule events emitted per camera.",
            base_labels,
        )
        self.reconnects_total = Counter(
            "ava_reconnects_total",
            "Source reconnect attempts per camera.",
            base_labels,
        )
        self.errors_total = Counter(
            "ava_errors_total",
            "Worker errors by kind per camera.",
            base_labels + ("kind",),
        )
        self.inference_duration_seconds = Histogram(
            "ava_inference_duration_seconds",
            "Inference call duration per camera.",
            base_labels,
            buckets=INFERENCE_DURATION_BUCKETS,
        )
        self.event_latency_seconds = Histogram(
            "ava_event_latency_seconds",
            "Frame-decode to event-emission latency per camera.",
            base_labels,
            buckets=EVENT_LATENCY_BUCKETS,
        )
        self.queue_depth = Gauge(
            "ava_queue_depth",
            "Per-camera frame buffer depth (sampled).",
            ("service", "worker", "camera"),
        )
        self.queue_age_seconds = Gauge(
            "ava_queue_age_seconds",
            "Age of the oldest buffered frame in seconds (sampled).",
            ("service", "worker", "camera"),
        )
        self.run_started = Gauge(
            "ava_run_started",
            "Run start timestamp (unix seconds); 0 when no run is active.",
            ("service", "run_id"),
        )

    # -- label guards -------------------------------------------------------

    def _check_camera(self, camera_id: str) -> None:
        inventory = self.context.camera_ids
        if inventory and camera_id not in inventory:
            raise BoundedLabelsError(
                f"camera {camera_id!r} is not in the configured inventory "
                f"{sorted(inventory)}; unbounded label values are forbidden"
            )

    def _labels(self, camera_id: str) -> dict[str, str]:
        self._check_camera(camera_id)
        return {
            "service": "ava",
            "worker": self.context.worker_id,
            "camera": camera_id,
        }

    # -- recording API ------------------------------------------------------

    def record_decode(self, camera_id: str) -> None:
        self.frames_decoded_total.labels(**self._labels(camera_id)).inc()

    def record_inference(self, camera_id: str, duration_s: float) -> None:
        labels = self._labels(camera_id)
        self.frames_inferred_total.labels(**labels).inc()
        self.inference_duration_seconds.labels(**labels).observe(duration_s)

    def record_drop(self, camera_id: str, reason: str) -> None:
        if reason not in DROP_REASONS:
            raise BoundedLabelsError(
                f"drop reason {reason!r} not in bounded set {DROP_REASONS}"
            )
        self.frames_dropped_total.labels(**self._labels(camera_id), reason=reason).inc()

    def record_event(self, camera_id: str, latency_s: float) -> None:
        labels = self._labels(camera_id)
        self.events_emitted_total.labels(**labels).inc()
        self.event_latency_seconds.labels(**labels).observe(latency_s)

    def record_reconnect(self, camera_id: str) -> None:
        self.reconnects_total.labels(**self._labels(camera_id)).inc()

    def record_error(self, camera_id: str, kind: str) -> None:
        if kind not in ERROR_KINDS:
            raise BoundedLabelsError(
                f"error kind {kind!r} not in bounded set {ERROR_KINDS}"
            )
        self.errors_total.labels(**self._labels(camera_id), kind=kind).inc()

    def sample_queue(self, camera_id: str, depth: int, age_s: float) -> None:
        labels = self._labels(camera_id)
        with self._lock:
            self.queue_depth.labels(**labels).set(depth)
            self.queue_age_seconds.labels(**labels).set(age_s)

    def mark_run_started(self) -> None:
        import time

        self.run_started.labels(service="ava", run_id=self.context.run_id).set(
            time.time()
        )


# ---------------------------------------------------------------------------
# Optional scrape endpoint
# ---------------------------------------------------------------------------


def serve_metrics(port: int, host: str = "127.0.0.1") -> threading.Thread:
    """Serve the Prometheus scrape endpoint on a daemon thread.

    Binds to ``host`` (loopback by default) so the study stack is not exposed
    to the LAN. Returns the started thread; the caller owns its lifetime. A
    bind failure raises OSError immediately so a busy port is visible at run
    start rather than silently dropping telemetry.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from prometheus_client import generate_latest

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            payload = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:  # silence request logging
            return

    server = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(
        target=server.serve_forever, name="ava-metrics-scraper", daemon=True
    )
    thread.start()
    return thread


def render_metrics() -> str:
    """Render the current registry in Prometheus text format (for tests)."""
    from io import StringIO

    from prometheus_client import generate_latest

    buf = StringIO()
    buf.write(generate_latest().decode("utf-8"))
    return buf.getvalue()
