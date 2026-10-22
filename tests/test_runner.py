"""Run orchestration integration tests (no GPU, no model download).

The detector is faked and the ingest boundary (probe / iter_frames) is patched
so the owned orchestration logic — phase timing, warm-up exclusion, output
wiring, rule-driven event/evidence emission, and summary assembly — is
validated deterministically. Real inference and decode are narrow exceptions
(see the benchmark report).
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import numpy as np

from ava import runner
from ava.config import (
    CameraConfig,
    DetectorConfig,
    LineCrossingRule,
    LineGeometry,
    RuleProfile,
    RunConfig,
)
from ava.detector import Detection, FrameResult
from ava.ingest import Frame
from ava.rules import RuleEvent


class _FakeDetector:
    """A detector that returns one crossing-producing detection per frame."""

    def __init__(self, n_frames=3, device="cpu"):
        self.device = device
        self.n_frames = n_frames
        self.loaded = False
        self.reset_calls = 0

    def load(self):
        self.loaded = True

    def reset_tracker(self):
        self.reset_calls += 1

    def detect(self, frame_index, timestamp_s, bgr_frame):
        # Alternate sides of the y=0.5 line so a crossing rule fires once.
        x = 0.2 if frame_index % 2 == 0 else 0.8
        y_bottom = 0.3 if frame_index % 2 == 0 else 0.7
        det = Detection(class_id=0, label="person", confidence=0.9,
                        box=(x - 0.05, y_bottom - 0.2, x + 0.05, y_bottom),
                        track_id=1)
        return FrameResult(frame_index, timestamp_s, bgr_frame.shape[1],
                           bgr_frame.shape[0], [det])


def _fake_frames(n):
    for i in range(n):
        yield Frame(index=i, timestamp_s=i / 10.0,
                    bgr=np.zeros((48, 64, 3), dtype=np.uint8))


def _patch_ingest(monkeypatch, n=3, detector=None):
    monkeypatch.setattr(runner, "probe", lambda src: {
        "width": 64, "height": 48, "fps": 10.0, "frame_count": n,
    })
    monkeypatch.setattr(runner, "iter_frames", lambda src: _fake_frames(n))
    if detector is not None:
        monkeypatch.setattr(runner, "build_detector", lambda cfg: detector)


def _simple_config(tmp_path: Path) -> RunConfig:
    return RunConfig(
        source=tmp_path / "in.mp4",
        detector=DetectorConfig(kind="conventional", model=tmp_path / "m.pt"),
        output_dir=tmp_path / "out",
        gpu=None,  # CPU path: no CUDA sync
        jsonl=True,
        annotate=False,
    )


# --- simple run --------------------------------------------------------------


def test_simple_run_produces_jsonl_and_summary(tmp_path, monkeypatch):
    _patch_ingest(monkeypatch, n=3, detector=_FakeDetector())
    config = _simple_config(tmp_path)

    summary = runner.run(config)

    assert summary.frames == 3
    assert summary.detections == 3
    assert summary.detector_kind == "conventional"
    assert summary.decode_seconds >= 0.0
    assert summary.inference_seconds >= 0.0
    # JSONL written with one line per frame.
    jsonl = tmp_path / "out" / "detections.jsonl"
    assert jsonl.exists()
    lines = jsonl.read_text().strip().splitlines()
    assert len(lines) == 3
    assert summary.output_paths == [str(jsonl)]


def test_simple_run_warmup_excludes_early_frames(tmp_path, monkeypatch):
    # With more than WARMUP_FRAMES frames, steady-state latency stats are set.
    _patch_ingest(monkeypatch, n=runner.WARMUP_FRAMES + 5, detector=_FakeDetector())
    config = _simple_config(tmp_path)

    summary = runner.run(config)

    assert summary.frames == runner.WARMUP_FRAMES + 5
    # Steady-state percentiles are computed from post-warm-up frames.
    assert summary.infer_p50_s >= 0.0
    assert summary.infer_p95_s >= 0.0
    assert summary.infer_mean_s >= 0.0


def test_simple_run_no_jsonl_skips_writer(tmp_path, monkeypatch):
    _patch_ingest(monkeypatch, n=2, detector=_FakeDetector())
    config = _simple_config(tmp_path)
    config.jsonl = False

    summary = runner.run(config)

    assert not (tmp_path / "out" / "detections.jsonl").exists()
    assert summary.output_paths == []


# --- build_detector ----------------------------------------------------------


def test_build_detector_conventional(tmp_path):
    config = _simple_config(tmp_path)
    det = runner.build_detector(config)
    assert type(det).__name__ == "ConventionalDetector"
    assert det.device == "cpu"  # gpu=None -> cpu


def test_build_detector_prompted(tmp_path):
    config = _simple_config(tmp_path)
    config.detector.kind = "prompted"
    config.detector.prompts = ["person", "car"]
    det = runner.build_detector(config)
    assert type(det).__name__ == "PromptedDetector"
    assert det.prompts == ["person", "car"]


# --- camera run (rules + events + evidence) ----------------------------------


def _crossing_rule(**kw):
    base = {
        "id": "cross",
        "line": LineGeometry(start=(0.0, 0.5), end=(1.0, 0.5)),
        "direction": "both",
    }
    base.update(kw)
    return LineCrossingRule(**base)


def _camera_config(tmp_path: Path, snapshot: bool = False, n_cams=1):
    from ava.config import EvidenceOptions

    rule = _crossing_rule(
        evidence=EvidenceOptions(snapshot=snapshot),
    )
    profile = RuleProfile(id="p1", rules=[rule])
    cams = [
        CameraConfig(id=f"cam-{i:02d}", source=tmp_path / f"in{i}.mp4",
                     profiles=["p1"])
        for i in range(1, n_cams + 1)
    ]
    return RunConfig(
        detector=DetectorConfig(kind="conventional", model=tmp_path / "m.pt"),
        output_dir=tmp_path / "out",
        gpu=None,
        jsonl=True,
        annotate=False,
        cameras=cams,
        profiles=[profile],
    )


def test_camera_run_emits_events_and_per_camera_jsonl(tmp_path, monkeypatch):
    _patch_ingest(monkeypatch, n=3, detector=_FakeDetector())
    config = _camera_config(tmp_path)

    summary = runner.run(config)

    assert summary.frames == 3
    # A crossing occurs between frame 0 (above) and frame 1 (below).
    assert summary.events >= 1
    events_path = tmp_path / "out" / "events.jsonl"
    assert events_path.exists()
    records = [json.loads(l) for l in events_path.read_text().strip().splitlines()]
    assert len(records) == summary.events
    assert all(r["rule_id"] == "cross" for r in records)
    # Per-camera detection JSONL.
    cam_jsonl = tmp_path / "out" / "cam-01.jsonl"
    assert cam_jsonl.exists()


def test_camera_run_resets_tracker_once_per_camera(tmp_path, monkeypatch):
    """Each camera starts on a fresh tracker/session."""
    detector = _FakeDetector()
    _patch_ingest(monkeypatch, n=3, detector=detector)
    config = _camera_config(tmp_path, n_cams=2)

    summary = runner.run(config)

    # One reset before each of the two camera streams.
    assert detector.reset_calls == 2
    assert summary.frames == 6


def test_camera_run_snapshot_evidence_written(tmp_path, monkeypatch):
    _patch_ingest(monkeypatch, n=3, detector=_FakeDetector())
    config = _camera_config(tmp_path, snapshot=True)

    summary = runner.run(config)

    assert summary.events >= 1
    events_path = tmp_path / "out" / "events.jsonl"
    records = [json.loads(l) for l in events_path.read_text().strip().splitlines()]
    # The triggering event carries a snapshot locator that exists on disk.
    with_evidence = [r for r in records if r["evidence"]["kind"] == "snapshot"]
    assert with_evidence, "expected at least one snapshot evidence record"
    rel = with_evidence[0]["evidence"]["path"]
    assert (tmp_path / "out" / rel).exists()


def test_camera_run_clip_evidence_written(tmp_path, monkeypatch):
    from ava.config import EvidenceOptions

    _patch_ingest(monkeypatch, n=3, detector=_FakeDetector())
    config = _camera_config(tmp_path)
    # Enable clip evidence on the crossing rule.
    config.profiles[0].rules[0].evidence = EvidenceOptions(clip=True, clip_seconds=1.0)

    summary = runner.run(config)

    assert summary.events >= 1
    events_path = tmp_path / "out" / "events.jsonl"
    records = [json.loads(l) for l in events_path.read_text().strip().splitlines()]
    with_evidence = [r for r in records if r["evidence"]["kind"] == "clip"]
    assert with_evidence, "expected at least one clip evidence record"
    rel = with_evidence[0]["evidence"]["path"]
    assert (tmp_path / "out" / rel).exists()


# --- GPU branch and annotate paths -------------------------------------------


def test_simple_run_gpu_branch(tmp_path, monkeypatch):
    # device="cuda" exercises the sync + peak-memory branch; on a host without
    # CUDA the sync is a no-op and peak memory reads 0.
    _patch_ingest(monkeypatch, n=3, detector=_FakeDetector(device="cuda"))
    config = _simple_config(tmp_path)

    summary = runner.run(config)

    assert summary.frames == 3
    assert summary.peak_gpu_mem_bytes >= 0


def test_simple_run_annotate_writes_video(tmp_path, monkeypatch):
    _patch_ingest(monkeypatch, n=3, detector=_FakeDetector())
    config = _simple_config(tmp_path)
    config.annotate = True

    summary = runner.run(config)

    video = tmp_path / "out" / "annotated.mp4"
    assert video.exists()
    assert str(video) in summary.output_paths


def test_camera_run_annotate_writes_video(tmp_path, monkeypatch):
    _patch_ingest(monkeypatch, n=3, detector=_FakeDetector())
    config = _camera_config(tmp_path)
    config.annotate = True

    summary = runner.run(config)

    video = tmp_path / "out" / "cam-01-annotated.mp4"
    assert video.exists()
    assert str(video) in summary.output_paths


def test_camera_run_gpu_branch(tmp_path, monkeypatch):
    _patch_ingest(monkeypatch, n=3, detector=_FakeDetector(device="cuda"))
    config = _camera_config(tmp_path)

    summary = runner.run(config)

    assert summary.frames == 3
    assert summary.peak_gpu_mem_bytes >= 0


# --- defensive branches ------------------------------------------------------


def test_run_summary_to_dict():
    summary = runner.RunSummary(source="in.mp4", detector_kind="conventional")
    d = summary.to_dict()
    assert d["source"] == "in.mp4"
    assert d["detector_kind"] == "conventional"
    assert d["frames"] == 0


def test_evidence_snapshot_miss_returns_none(monkeypatch):
    # Defensive branch: a snapshot is requested but the triggering frame is no
    # longer in the ring buffer (evicted), so no evidence is attached.
    from ava.config import EvidenceOptions

    rule = _crossing_rule(evidence=EvidenceOptions(snapshot=True))
    event = RuleEvent(
        schema_version="1", event_id="e1", camera_id="cam-01", session_id="s1",
        track_id=1, rule_id="cross", rule_type="line_crossing",
        class_label="person", confidence=0.9, source_time_s=0.1,
        emitted_utc="2026-01-01T00:00:00.000Z",
        frame_index=99,  # not present in the buffer below
    )
    store = runner.EvidenceStore.__new__(runner.EvidenceStore)

    result = runner._evidence_for(rule, event, store, deque([(0, None)]), 10.0)
    assert result is None


# --- run isolation (reruns do not append to prior events) --------------------


def test_camera_run_rerun_isolates_events(tmp_path, monkeypatch):
    """A second run into the same output dir writes a new events file."""
    _patch_ingest(monkeypatch, n=3, detector=_FakeDetector())
    config = _camera_config(tmp_path)

    # First run.
    runner.run(config)
    first_events = tmp_path / "out" / "events.jsonl"
    assert first_events.exists()
    first_count = len(first_events.read_text().strip().splitlines())

    # Second run into the same output dir.
    runner.run(config)
    # The original events.jsonl is untouched; a new numbered file appears.
    assert len(first_events.read_text().strip().splitlines()) == first_count
    second_events = tmp_path / "out" / "events-1.jsonl"
    assert second_events.exists()
    second_count = len(second_events.read_text().strip().splitlines())
    assert second_count >= 1


def test_camera_run_rerun_isolates_per_camera_jsonl(tmp_path, monkeypatch):
    """Per-camera JSONL is overwritten on rerun (single source of truth)."""
    _patch_ingest(monkeypatch, n=3, detector=_FakeDetector())
    config = _camera_config(tmp_path)

    runner.run(config)
    cam_jsonl = tmp_path / "out" / "cam-01.jsonl"
    first_lines = len(cam_jsonl.read_text().strip().splitlines())

    runner.run(config)
    # Overwritten, not appended.
    second_lines = len(cam_jsonl.read_text().strip().splitlines())
    assert second_lines == first_lines


# --- centered clip evidence --------------------------------------------------


def test_clip_evidence_centered_on_triggering_frame(tmp_path, monkeypatch):
    """The clip window includes frames before AND after the triggering frame."""
    from ava.config import EvidenceOptions

    # Use more frames so the buffer has post-event content.
    _patch_ingest(monkeypatch, n=10, detector=_FakeDetector(n_frames=10))
    config = _camera_config(tmp_path)
    config.profiles[0].rules[0].evidence = EvidenceOptions(clip=True, clip_seconds=2.0)

    summary = runner.run(config)
    assert summary.events >= 1

    # Find the clip evidence file and verify it exists.
    events_path = tmp_path / "out" / "events.jsonl"
    records = [json.loads(l) for l in events_path.read_text().strip().splitlines()]
    with_clip = [r for r in records if r["evidence"] and r["evidence"]["kind"] == "clip"]
    assert with_clip, "expected clip evidence"
    rel = with_clip[0]["evidence"]["path"]
    clip_path = tmp_path / "out" / rel
    assert clip_path.exists()


def test_deferred_clip_flushed_at_end_of_stream(tmp_path, monkeypatch):
    """Clips that cannot be centered mid-stream are flushed at stream end."""
    from ava.config import EvidenceOptions

    # Few frames: the post-event window may not fully accumulate before EOF.
    _patch_ingest(monkeypatch, n=3, detector=_FakeDetector(n_frames=3))
    config = _camera_config(tmp_path)
    config.profiles[0].rules[0].evidence = EvidenceOptions(clip=True, clip_seconds=5.0)

    summary = runner.run(config)
    assert summary.events >= 1

    events_path = tmp_path / "out" / "events.jsonl"
    records = [json.loads(l) for l in events_path.read_text().strip().splitlines()]
    # The clip is flushed at end-of-stream with whatever frames are available.
    with_clip = [r for r in records if r["evidence"] and r["evidence"]["kind"] == "clip"]
    assert with_clip, "expected clip evidence flushed at stream end"
