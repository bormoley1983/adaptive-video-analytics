"""Compare batch predictions on identical decoded images and synthetic events."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path

from ava.batch import BatchItem, BatchItemResult, BatchKey, DetectionBatch
from ava.batch_backend import ByteTrackCameraTracker, UltralyticsBatchDetector
from ava.config import DetectorConfig, LineCrossingRule
from ava.detector import Detection
from ava.ingest import Frame, iter_frames
from ava.rules import RuleEngine


def iou(a, b):
    intersection = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(
        0., min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union else 0.


def compare(reference, candidate):
    if len(reference) != len(candidate):
        return {"passed": False, "reason": "count differs"}
    edges = [[j for j, b in enumerate(candidate)
              if a.class_id == b.class_id and iou(a.box, b.box) >= .99
              and abs(a.confidence - b.confidence) <= .001] for a in reference]
    matched = {}
    def assign(index, seen):
        for other in edges[index]:
            if other in seen:
                continue
            seen.add(other)
            if other not in matched or assign(matched[other], seen):
                matched[other] = index
                return True
        return False
    if not all(assign(index, set()) for index in range(len(reference))):
        return {"passed": False, "reason": "class/box/confidence matching failed"}
    return {"passed": True,
            "min_iou": min((iou(reference[i].box, candidate[j].box)
                            for j, i in matched.items()), default=None),
            "max_confidence_delta": max((abs(reference[i].confidence - candidate[j].confidence)
                                         for j, i in matched.items()), default=None)}


def synthetic_events():
    import numpy as np
    rule = LineCrossingRule(id="crossing", line={"start": (0., .5), "end": (1., .5)})
    traces = []
    for reverse in (False, True):
        ids = []
        for camera in ("a", "b"):
            tracker = ByteTrackCameraTracker(10)
            tracker.reset("synthetic")
            engine = RuleEngine([rule], camera, "synthetic")
            for index in range(40):
                bottom = .3 + index * .01 if not reverse else .3
                frame = Frame(index, index / 10, np.zeros((100, 100, 3), dtype=np.uint8),
                              session_id="synthetic")
                item = BatchItem(BatchKey(camera, "synthetic", index), frame, 0.)
                detected = BatchItemResult(item.key, 100, 100,
                    (Detection(0, "person", .9, (.4, bottom - .2, .6, bottom)),))
                tracked = tracker.update(item, detected)
                ids.extend(e.event_id for e in engine.process_frame(tracked, "2026-01-01T00:00:00Z"))
        traces.append(ids)
    return {"positive": traces[0], "negative": traces[1]}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    frames = []
    for frame in iter_frames(args.source):
        if frame.index % 8 == 0:
            frames.append(frame)
        if len(frames) == 32:
            break
    if len(frames) != 32:
        raise ValueError("source must supply 32 samples at stride eight")
    backend = UltralyticsBatchDetector(DetectorConfig(model=args.model))
    report = {"model_sha256": sha256(args.model), "source_sha256": sha256(args.source),
              "frames": [{"index": f.index, "sha256": hashlib.sha256(f.bgr.tobytes()).hexdigest()}
                         for f in frames], "comparisons": []}
    def make_batch(selected):
        return DetectionBatch(tuple(BatchItem(BatchKey(f"sample-{f.index}", "parity", f.index),
            Frame(f.index, f.timestamp_s, f.bgr, session_id="parity"), time.monotonic())
            for f in selected))
    try:
        backend.load()
        reference = [backend.detect_batch(make_batch([f]))[0] for f in frames]
        for size in (4, 8, 16, 32):
            candidate = []
            for start in range(0, len(frames), size):
                candidate.extend(backend.detect_batch(make_batch(frames[start:start + size])))
            for expected, actual in zip(reference, candidate, strict=True):
                verdict = compare(expected.detections, actual.detections)
                report["comparisons"].append({"batch_size": size, "key": asdict(expected.key),
                    "reference": asdict(expected), "candidate": asdict(actual), **verdict})
        trace = synthetic_events()
        repeat = synthetic_events()
        report["synthetic"] = {"trace": trace, "repeat": repeat,
                               "passed": trace == repeat and len(trace["positive"]) == 2
                               and not trace["negative"]}
        report["passed"] = (all(v["passed"] for v in report["comparisons"])
                            and report["synthetic"]["passed"])
    finally:
        backend.close()
        report["arithmetic"] = backend.arithmetic
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps({"passed": report["passed"], "comparisons": len(report["comparisons"])}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
