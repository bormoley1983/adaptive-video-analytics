"""Tests for the versioned event/evidence contract (ADR-5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ava.config import (
    CONFIG_SCHEMA_VERSION,
    LineCrossingRule,
    LineGeometry,
)
from ava.events import EventRecord, EventWriter, EvidenceLocator, EvidenceStore
from ava.rules import RuleEngine, RuleEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _crossing_rule(**kw) -> LineCrossingRule:
    defaults = {
        "id": "cross",
        "line": LineGeometry(start=(0.0, 0.5), end=(1.0, 0.5)),
        "direction": "both",
        "deadband": 0.0,
        "debounce_frames": 0,
    }
    defaults.update(kw)
    return LineCrossingRule(**defaults)


def _make_event(event_id: str = "cam-01/s1/t1/cross/crossing-0", **kw) -> RuleEvent:
    defaults = {
        "schema_version": "1",
        "event_id": event_id,
        "camera_id": "cam-01",
        "session_id": "s1",
        "track_id": 1,
        "rule_id": "cross",
        "rule_type": "line_crossing",
        "class_label": "person",
        "confidence": 0.92,
        "source_time_s": 12.34,
        "emitted_utc": "2026-01-01T00:00:00Z",
        "payload": {"from_side": -1, "to_side": 1},
    }
    defaults.update(kw)
    return RuleEvent(**defaults)


# ---------------------------------------------------------------------------
# EventRecord
# ---------------------------------------------------------------------------

class TestEventRecord:
    def test_from_rule_event_carries_all_adr5_fields(self):
        ev = _make_event()
        rec = EventRecord.from_rule_event(
            ev, config_version=CONFIG_SCHEMA_VERSION, model_ref="yolo26n.pt"
        )
        assert rec.schema_version == "1"
        assert rec.event_id == "cam-01/s1/t1/cross/crossing-0"
        assert rec.camera_id == "cam-01"
        assert rec.session_id == "s1"
        assert rec.track_id == 1
        assert rec.rule_id == "cross"
        assert rec.rule_type == "line_crossing"
        assert rec.class_label == "person"
        assert rec.confidence == pytest.approx(0.92)
        assert rec.source_time_s == pytest.approx(12.34)
        assert rec.emitted_utc == "2026-01-01T00:00:00Z"
        assert rec.config_version == CONFIG_SCHEMA_VERSION
        assert rec.model_ref == "yolo26n.pt"
        assert rec.evidence.kind is None
        assert rec.payload == {"from_side": -1, "to_side": 1}

    def test_to_json_roundtrip(self):
        ev = _make_event()
        rec = EventRecord.from_rule_event(
            ev, config_version="1", model_ref="m.pt",
            evidence=EvidenceLocator(kind="snapshot", path="evidence/cam-01/x.jpg"),
        )
        data = json.loads(rec.to_json())
        assert data["event_id"] == rec.event_id
        assert data["evidence"]["kind"] == "snapshot"
        assert data["evidence"]["path"] == "evidence/cam-01/x.jpg"
        # JSON is deterministic (sorted keys) for stable diffs.
        assert rec.to_json() == json.dumps(data, sort_keys=True)

    def test_ids_distinguish_separate_incidents(self):
        ev_a = _make_event(event_id="cam-01/s1/t1/cross/crossing-0")
        ev_b = _make_event(event_id="cam-01/s1/t1/cross/crossing-1")
        ev_c = _make_event(event_id="cam-02/s1/t1/cross/crossing-0", camera_id="cam-02")
        recs = [EventRecord.from_rule_event(e, config_version="1", model_ref=None)
                for e in (ev_a, ev_b, ev_c)]
        ids = {r.event_id for r in recs}
        assert len(ids) == 3


# ---------------------------------------------------------------------------
# EventWriter: idempotent IDs across retries
# ---------------------------------------------------------------------------

class TestEventWriter:
    def test_append_and_read_back(self, tmp_path: Path):
        path = tmp_path / "events.jsonl"
        writer = EventWriter(path)
        rec = EventRecord.from_rule_event(_make_event(), config_version="1", model_ref=None)
        assert writer.write(rec) is True
        writer.close()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["event_id"] == rec.event_id

    def test_retry_preserves_same_event_id(self, tmp_path: Path):
        """Two runs over the same stream produce the same event IDs."""
        path = tmp_path / "events.jsonl"
        # First run.
        w1 = EventWriter(path)
        engine1 = RuleEngine([_crossing_rule()], camera_id="cam-01", session_id="s1")
        from tests.test_rules_engine import det
        from tests.test_rules_engine import frame as mkframe
        frames = [
            mkframe(0, 0.0, [det(1, 0.2, 0.3)]),
            mkframe(1, 0.1, [det(1, 0.8, 0.7)]),
        ]
        ids_run1 = []
        for f in frames:
            for ev in engine1.process_frame(f, emitted_utc="syn"):
                ids_run1.append(ev.event_id)
                w1.write(EventRecord.from_rule_event(ev, config_version="1", model_ref=None))
        w1.close()

        # Retry (same stream, fresh writer appending to same file).
        w2 = EventWriter(path)
        engine2 = RuleEngine([_crossing_rule()], camera_id="cam-01", session_id="s1")
        ids_run2 = []
        for f in frames:
            for ev in engine2.process_frame(f, emitted_utc="syn"):
                ids_run2.append(ev.event_id)
                w2.write(EventRecord.from_rule_event(ev, config_version="1", model_ref=None))
        w2.close()

        assert ids_run1 == ids_run2  # deterministic IDs
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2  # both runs appended; consumers dedupe on event_id

    def test_dedupe_mode_skips_existing_ids(self, tmp_path: Path):
        path = tmp_path / "events.jsonl"
        rec = EventRecord.from_rule_event(_make_event(), config_version="1", model_ref=None)
        w1 = EventWriter(path)
        w1.write(rec)
        w1.close()

        # Crash-recovery: reopen with dedupe, same event is skipped.
        w2 = EventWriter(path, dedupe=True)
        assert w2.write(rec) is False  # duplicate
        # A new event still writes.
        rec2 = EventRecord.from_rule_event(
            _make_event(event_id="cam-01/s1/t1/cross/crossing-1"),
            config_version="1", model_ref=None,
        )
        assert w2.write(rec2) is True
        w2.close()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert [json.loads(l)["event_id"] for l in lines] == [
            "cam-01/s1/t1/cross/crossing-0",
            "cam-01/s1/t1/cross/crossing-1",
        ]

    def test_dedupe_rebuild_skips_malformed_lines(self, tmp_path: Path):
        """Crash recovery tolerates a truncated/partial line in the existing
        output: malformed lines are ignored while valid IDs are still deduped."""
        path = tmp_path / "events.jsonl"
        rec = EventRecord.from_rule_event(_make_event(), config_version="1", model_ref=None)
        # Pre-existing file with one valid ID, one truncated line and a blank.
        path.write_text(
            json.dumps({"event_id": rec.event_id}) + "\n"
            + '{"event_id": "cam-01/s1/t1/cross/cros'  # truncated JSON
            + "\n\n",
            encoding="utf-8",
        )
        writer = EventWriter(path, dedupe=True)
        assert writer.write(rec) is False  # valid ID still recognized
        rec2 = EventRecord.from_rule_event(
            _make_event(event_id="cam-01/s1/t1/cross/crossing-1"),
            config_version="1", model_ref=None,
        )
        assert writer.write(rec2) is True  # new ID writes
        writer.close()

        lines = path.read_text().strip().splitlines()
        # original valid + truncated + blank + newly appended
        assert len(lines) == 4


# ---------------------------------------------------------------------------
# EvidenceStore
# ---------------------------------------------------------------------------

class TestEvidenceStore:
    def test_snapshot_writes_file_and_locator(self, tmp_path: Path):
        import numpy as np

        store = EvidenceStore(tmp_path)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        loc = store.save_snapshot("cam-01", "ev-123", frame)
        assert loc.kind == "snapshot"
        assert loc.path == "evidence/cam-01/ev-123.jpg"
        assert (tmp_path / loc.path).exists()

    def test_clip_caps_duration(self, tmp_path: Path):
        import numpy as np

        store = EvidenceStore(tmp_path)
        fps = 10.0
        # 5 frames at 10 fps = 0.5 s; clip_seconds=0.2 caps to 2 frames.
        frames = [(i, np.zeros((48, 64, 3), dtype=np.uint8)) for i in range(5)]
        loc = store.save_clip("cam-01", "ev-clip", frames, fps=fps, clip_seconds=0.2)
        assert loc.kind == "clip"
        assert loc.path == "evidence/cam-01/ev-clip.mp4"
        assert (tmp_path / loc.path).exists()

    def test_empty_clip_returns_no_evidence(self, tmp_path: Path):
        store = EvidenceStore(tmp_path)
        loc = store.save_clip("cam-01", "ev-empty", [], fps=10.0, clip_seconds=1.0)
        assert loc.kind is None
        assert loc.path is None

    def test_snapshot_write_failure_raises(self, tmp_path: Path, monkeypatch):
        import cv2
        import numpy as np

        store = EvidenceStore(tmp_path)
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        monkeypatch.setattr(cv2, "imwrite", lambda *a, **k: False)
        with pytest.raises(RuntimeError, match="failed to write snapshot"):
            store.save_snapshot("cam-01", "ev-fail", frame)

    def test_clip_writer_not_opened_raises(self, tmp_path: Path, monkeypatch):
        import cv2
        import numpy as np

        store = EvidenceStore(tmp_path)
        frames = [(0, np.zeros((48, 64, 3), dtype=np.uint8))]

        class _BrokenWriter:
            def isOpened(self):
                return False

            def write(self, *a, **k):
                raise AssertionError("write must not be called")

            def release(self):
                pass

        monkeypatch.setattr(cv2, "VideoWriter", lambda *a, **k: _BrokenWriter())
        with pytest.raises(RuntimeError, match="cannot open clip writer"):
            store.save_clip("cam-01", "ev-fail", frames, fps=10.0, clip_seconds=1.0)


# ---------------------------------------------------------------------------
# Component integration: rule engine to persisted evidence
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestEventPipelineIntegration:
    def test_engine_event_becomes_record_with_evidence(self, tmp_path: Path):
        import numpy as np

        from tests.test_rules_engine import det
        from tests.test_rules_engine import frame as mkframe

        rule = _crossing_rule()
        engine = RuleEngine([rule], camera_id="cam-01", session_id="s1")
        frames = [
            mkframe(0, 0.0, [det(1, 0.2, 0.3)]),
            mkframe(1, 0.1, [det(1, 0.8, 0.7)]),
        ]
        store = EvidenceStore(tmp_path)
        writer = EventWriter(tmp_path / "events.jsonl")

        for f in frames:
            bgr = np.zeros((480, 640, 3), dtype=np.uint8)
            for ev in engine.process_frame(f, emitted_utc="2026-01-01T00:00:00Z"):
                loc = store.save_snapshot("cam-01", ev.event_id, bgr)
                rec = EventRecord.from_rule_event(
                    ev, config_version=CONFIG_SCHEMA_VERSION,
                    model_ref="yolo26n.pt", evidence=loc,
                )
                writer.write(rec)
        writer.close()

        lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["event_id"] == "cam-01/s1/t1/cross/crossing-0"
        assert data["evidence"]["kind"] == "snapshot"
        assert (tmp_path / data["evidence"]["path"]).exists()
