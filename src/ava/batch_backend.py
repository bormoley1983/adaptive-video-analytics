"""Detection-only GPU backend and camera-local ByteTrack adapter."""

import time
from dataclasses import replace
from types import SimpleNamespace

from .batch import BatchItemResult
from .batch_precision import configure_precision, precision_settings, restore_precision
from .detector import Detection, FrameResult, _resolve_device
from .stage_metrics import stage_span


def validate_backend(live, detector):
    if live.inference_backend == "deepstream":
        raise NotImplementedError("DeepStream live adapter is not implemented")
    if live.detector_mode == "batched" and detector.kind != "conventional":
        raise ValueError("batched inference requires a conventional detector")


class UltralyticsBatchDetector:
    def __init__(self, config, gpu="0", *, recorder=None, profile_device=False):
        self.recorder = recorder
        self.profile_device = bool(profile_device) and recorder is not None
        self._device_stream = None
        self._stage_originals = {}
        self._current_batch_items = ()
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
        # Warm-up runs before profiling is initialized: no selection, no events,
        # no stream. The device stream and stage hooks are created after warm-up
        # so the first measured batch is a real inference call, not a warm-up.
        self._predict([np.zeros((640, 640, 3), dtype=np.uint8)])
        if self.recorder is not None:
            # Device profiling requires CUDA; check before attaching hooks so a
            # CPU device with profile_device=True fails fast without side effects.
            if self.profile_device and self.device != "cuda":
                raise RuntimeError("device profiling requires a CUDA device")
            self._attach_stage_hooks()
            if self.profile_device:
                self._device_stream = torch.cuda.Stream(device=self.device)

    def _attach_stage_hooks(self):
        predictor = getattr(self.model, "predictor", None)
        if predictor is None:
            raise RuntimeError("stage profiling requires a warmed predictor")
        names = ("preprocess", "inference", "postprocess")
        if not all(callable(getattr(predictor, name, None)) for name in names):
            raise RuntimeError("predictor stage hooks are unavailable")
        for name in names:
            original = getattr(predictor, name)
            self._stage_originals[name] = (predictor, original)

            def measured(*args, _original=original, _name=name, **kwargs):
                with stage_span(self.recorder, "predictor_" + _name + "_host_wall"):
                    return _original(*args, **kwargs)
            setattr(predictor, name, measured)

    def _check_precision(self):
        if self.device == "cuda":
            current = precision_settings(self._torch)
            if any(value != "ieee" for value in current.values()):
                raise RuntimeError("GPU arithmetic policy changed during execution")

    def _predict(self, frames):
        self._check_precision()
        # Profiling is only active after load() has created the device stream.
        # Warm-up calls (before stream creation) take the plain path: no
        # selection, no events, no synchronization.
        if not self.profile_device or self._device_stream is None:
            results = self.model.predict(frames, conf=self.config.confidence,
                                         iou=self.config.iou, imgsz=640, rect=False,
                                         device=self.device, verbose=False)
            self._check_precision()
            return results
        # Device profiling: CUDA-event brackets around the prediction call on
        # the declared stream. Selection happens before any event operation or
        # synchronization; unselected batches perform zero measurement ops.
        recorder = self.recorder
        ordinal = recorder.select_device_batch()
        if ordinal is None:
            results = self.model.predict(frames, conf=self.config.confidence,
                                         iou=self.config.iou, imgsz=640, rect=False,
                                         device=self.device, verbose=False)
            self._check_precision()
            return results
        batch_id = f"batch-{ordinal}"
        camera_keys = tuple(
            getattr(getattr(item, "key", None), "camera_id", None) or str(i)
            for i, item in enumerate(self._current_batch_items))
        torch = self._torch
        stream_obj = self._device_stream
        status = "available"
        h2d_interval_s = inference_interval_s = sync_host_wall_s = None
        host_started_at = time.monotonic()
        # Measurement setup: event creation and start-event recording on the
        # declared stream. A failure here is recorded as unavailable but does
        # NOT prevent prediction — the batch is still predicted once (on the
        # previously current stream, since the context was never entered).
        events_ready = False
        h2d_start = h2d_end = inference_start = inference_end = None
        try:
            with torch.cuda.stream(stream_obj):
                h2d_start = torch.cuda.Event(enable_timing=True)
                h2d_end = torch.cuda.Event(enable_timing=True)
                inference_start = torch.cuda.Event(enable_timing=True)
                inference_end = torch.cuda.Event(enable_timing=True)
                h2d_start.record()
                inference_start.record()
                events_ready = True
        except Exception as exc:  # noqa: BLE001 - measurement setup must not break detection
            events_ready = False
            status = f"unavailable; event setup failed: {type(exc).__name__}: {exc}"
        # Prediction: the real inference call. When events are ready it runs
        # inside the declared stream context so CUDA events bracket its GPU
        # work; when setup failed it runs on the previously current stream.
        # A prediction failure is re-raised after recording the sample.
        outputs = None
        prediction_failed = False
        prediction_exc = None
        prediction_attempted = False
        if events_ready:
            try:
                with torch.cuda.stream(stream_obj):
                    prediction_attempted = True
                    try:
                        outputs = self.model.predict(frames, conf=self.config.confidence,
                                                     iou=self.config.iou, imgsz=640, rect=False,
                                                     device=self.device, verbose=False)
                    except Exception as exc:  # noqa: BLE001 - record then re-raise prediction failure
                        prediction_failed = True
                        prediction_exc = exc
                        status = f"unavailable; prediction failed: {type(exc).__name__}: {exc}"
                    # End-event recording: a failure here is a measurement error,
                    # NOT a prediction failure — the prediction succeeded and its
                    # result is valid. The sample is recorded as unavailable.
                    if not prediction_failed:
                        try:
                            inference_end.record()
                            h2d_end.record()
                        except Exception as exc:  # noqa: BLE001 - end-event failure is measurement-only
                            status = f"unavailable; end event failed: {type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - stream context failure is measurement-only
                if status == "available":
                    status = f"unavailable; stream context failed: {type(exc).__name__}: {exc}"
        if not prediction_attempted:
            # Event setup failed: predict without measurement so the batch is
            # still processed. No events, no synchronization for this batch.
            try:
                outputs = self.model.predict(frames, conf=self.config.confidence,
                                             iou=self.config.iou, imgsz=640, rect=False,
                                             device=self.device, verbose=False)
            except Exception as exc:  # noqa: BLE001 - record then re-raise prediction failure
                prediction_failed = True
                prediction_exc = exc
                status = f"unavailable; prediction failed: {type(exc).__name__}: {exc}"
        # Event read and synchronization: at most one sync per selected batch,
        # only when events are ready, prediction succeeded, AND end events were
        # successfully recorded (otherwise elapsed_time would read stale values).
        if events_ready and not prediction_failed and status == "available":
            try:
                sync_started = time.monotonic()
                stream_obj.synchronize()
                sync_host_wall_s = time.monotonic() - sync_started
                if not all(event.query() for event in
                           (h2d_start, h2d_end, inference_start, inference_end)):
                    raise RuntimeError("CUDA timing events are incomplete after synchronization")
                h2d_interval_s = h2d_start.elapsed_time(h2d_end) / 1000.
                inference_interval_s = inference_start.elapsed_time(inference_end) / 1000.
            except Exception as exc:  # noqa: BLE001 - event read must not break detection
                if status == "available":
                    status = f"unavailable; event read failed: {type(exc).__name__}: {exc}"
        host_finished_at = time.monotonic()
        recorder.record_device_batch(
            batch_id=batch_id, size=len(frames), camera_keys=camera_keys,
            ordinal=ordinal,
            stream=str(stream_obj) if stream_obj is not None else None,
            h2d_interval_s=h2d_interval_s, inference_interval_s=inference_interval_s,
            sync_host_wall_s=sync_host_wall_s, status=status,
            host_started_at=host_started_at, host_finished_at=host_finished_at)
        if prediction_failed:
            raise prediction_exc  # re-raise the original prediction exception
        self._check_precision()
        return outputs

    def detect_batch(self, batch):
        if self.model is None:
            raise RuntimeError("backend is not loaded")
        self._current_batch_items = batch.items
        with stage_span(self.recorder, "prediction_host_wall"):
            outputs = self._predict([item.frame.bgr for item in batch.items])
        if len(outputs) != len(batch.items):
            raise ValueError("backend result count differs from input")
        with stage_span(self.recorder, "result_conversion_host_wall"):
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
        for name, (predictor, original) in self._stage_originals.items():
            setattr(predictor, name, original)
        self._stage_originals.clear()
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
