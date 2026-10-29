"""Detection-only GPU backend and camera-local ByteTrack adapter."""

from dataclasses import replace
from types import SimpleNamespace

from .batch import BatchItemResult
from .batch_precision import configure_precision, precision_settings, restore_precision
from .detector import Detection, FrameResult, _resolve_device


def validate_backend(live, detector):
    if live.inference_backend == "deepstream":
        raise NotImplementedError("DeepStream live adapter is not implemented")
    if live.detector_mode == "batched" and detector.kind != "conventional":
        raise ValueError("batched inference requires a conventional detector")


class UltralyticsBatchDetector:
    def __init__(self, config, gpu="0"):
        self.config = config
        self.device = _resolve_device(gpu)
        self.model = None
        self.arithmetic = {}
        self._torch = None

    def load(self):
        if self.model is not None:
            return
        import torch
        from ultralytics import YOLO

        self._torch = torch
        if self.device == "cuda":
            self.arithmetic["before"] = configure_precision(torch)
            self.arithmetic["effective"] = precision_settings(torch)
        self.model = YOLO(str(self.config.model))
        self.model.to(self.device)
        import numpy as np
        self._predict([np.zeros((640, 640, 3), dtype=np.uint8)])

    def _check_precision(self):
        if self.device == "cuda":
            current = precision_settings(self._torch)
            if any(value != "ieee" for value in current.values()):
                raise RuntimeError("GPU arithmetic policy changed during execution")

    def _predict(self, frames):
        self._check_precision()
        results = self.model.predict(frames, conf=self.config.confidence,
                                     iou=self.config.iou, imgsz=640, rect=False,
                                     device=self.device, verbose=False)
        self._check_precision()
        return results

    def detect_batch(self, batch):
        if self.model is None:
            raise RuntimeError("backend is not loaded")
        outputs = self._predict([item.frame.bgr for item in batch.items])
        if len(outputs) != len(batch.items):
            raise ValueError("backend result count differs from input")
        results = []
        for item, output in zip(batch.items, outputs, strict=True):
            height, width = item.frame.bgr.shape[:2]
            detections = []
            for row in output.boxes.data.cpu().tolist():
                x1, y1, x2, y2, confidence, class_id = row
                class_id = int(class_id)
                box = tuple(max(0., min(1., v)) for v in
                            (x1 / width, y1 / height, x2 / width, y2 / height))
                detections.append(Detection(class_id, output.names[class_id], confidence, box))
            results.append(BatchItemResult(item.key, width, height, tuple(detections)))
        return tuple(results)

    def close(self):
        self.model = None
        if "before" in self.arithmetic:
            restore_precision(self._torch, self.arithmetic["before"])
            self.arithmetic["restored"] = precision_settings(self._torch)


class ByteTrackCameraTracker:
    def __init__(self, fps=30):
        self.fps = fps
        self.session = None
        self.last_index = -1
        self.last_timestamp = -1.
        self.tracker = None

    def reset(self, session_id):
        if not session_id:
            raise ValueError("session identity required")
        from ultralytics.trackers.byte_tracker import BYTETracker

        args = SimpleNamespace(track_high_thresh=.25, track_low_thresh=.1,
                               new_track_thresh=.25, track_buffer=30,
                               match_thresh=.8, fuse_score=True)
        self.tracker = BYTETracker(args)
        self.session = session_id
        self.last_index = -1
        self.last_timestamp = -1.

    def update(self, item, result):
        if result.failure or result.key != item.key:
            raise ValueError("tracker requires a successful correlated result")
        if item.key.session_id != self.session:
            raise ValueError("tracker session must be reset before delivery")
        if item.key.frame_index <= self.last_index or item.frame.timestamp_s < self.last_timestamp:
            raise ValueError("tracker input is out of order")
        import numpy as np
        from ultralytics.engine.results import Boxes

        rows = [[d.box[0] * result.width, d.box[1] * result.height,
                 d.box[2] * result.width, d.box[3] * result.height,
                 d.confidence, d.class_id] for d in result.detections]
        boxes = Boxes(np.asarray(rows, dtype=np.float32).reshape(-1, 6),
                      (result.height, result.width))
        tracked = self.tracker.update(boxes, item.frame.bgr)
        detections = []
        for row in tracked:
            x1, y1, x2, y2, track_id, confidence, _class_id, index = row
            original = result.detections[int(index)]
            box = tuple(max(0., min(1., float(v))) for v in
                        (x1 / result.width, y1 / result.height,
                         x2 / result.width, y2 / result.height))
            detections.append(replace(original, box=box, confidence=float(confidence),
                                      track_id=int(track_id)))
        self.last_index = item.key.frame_index
        self.last_timestamp = item.frame.timestamp_s
        return FrameResult(item.key.frame_index, item.frame.timestamp_s,
                           result.width, result.height, detections)
