"""Detector adapters.

A narrow contract: given a BGR frame, return detection metadata. The two E-1
paths (conventional YOLO26 and text-prompted YOLOE) share this shape so the
runner and output writers do not care which model produced the boxes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Detection:
    """A single detected object in one frame."""

    class_id: int
    label: str
    confidence: float
    # Normalised [x1, y1, x2, y2] in 0..1 relative to the frame.
    box: tuple[float, float, float, float]
    track_id: int | None = None


@dataclass(frozen=True)
class FrameResult:
    """All detections for one decoded frame."""

    index: int
    timestamp_s: float
    width: int
    height: int
    detections: list[Detection] = field(default_factory=list)


def _resolve_device(gpu: str | None) -> str:
    """Map a GPU selector (index or UUID) to a torch device string.

    Compose restricts visibility via CUDA_VISIBLE_DEVICES, so inside the
    container the selected GPU is always ordinal 0. A bare index or UUID both
    resolve to `cuda`; None falls back to CPU for smoke checks.
    """
    if gpu is None:
        return "cpu"
    return "cuda"


class Detector:
    """Common interface implemented by each inference backend."""

    def __init__(self, model_path: Path, device: str | None = "0") -> None:
        self.model_path = model_path
        self.device = _resolve_device(device)

    def load(self) -> None:
        """Load weights and any text-encoder assets. Idempotent."""
        raise NotImplementedError

    def detect(self, frame_index: int, timestamp_s: float, bgr_frame) -> FrameResult:
        """Run inference on one BGR frame and return metadata."""
        raise NotImplementedError


class ConventionalDetector(Detector):
    """YOLO26n/s conventional detection with ByteTrack."""

    def __init__(self, model_path: Path, device: str | None = "0",
                 confidence: float = 0.35, iou: float = 0.5) -> None:
        super().__init__(model_path, device)
        self.confidence = confidence
        self.iou = iou
        self._model = None

    def load(self) -> None:
        if self._model is not None:
            return
        from ultralytics import YOLO  # deferred: heavy import

        self._model = YOLO(str(self.model_path))
        # Warm the CUDA context so first-frame timing is not skewed.
        self._model.to(self.device)

    def detect(self, frame_index: int, timestamp_s: float, bgr_frame) -> FrameResult:
        assert self._model is not None, "call load() first"
        results = self._model.predict(
            bgr_frame,
            conf=self.confidence,
            iou=self.iou,
            tracker="bytetrack.yaml",
            verbose=False,
        )
        detections: list[Detection] = []
        if results:
            res = results[0]
            h, w = bgr_frame.shape[:2]
            boxes = res.boxes
            for i in range(len(boxes)):
                x1, y1, x2, y2 = (float(v) for v in boxes.xyxy[i].tolist())
                cls_id = int(boxes.cls[i])
                conf = float(boxes.conf[i])
                label = res.names.get(cls_id, str(cls_id))
                track_id = None
                if boxes.id is not None:
                    track_id = int(boxes.id[i])
                detections.append(
                    Detection(
                        class_id=cls_id,
                        label=label,
                        confidence=conf,
                        box=(x1 / w, y1 / h, x2 / w, y2 / h),
                        track_id=track_id,
                    )
                )
        return FrameResult(frame_index, timestamp_s, bgr_frame.shape[1],
                           bgr_frame.shape[0], detections)


class PromptedDetector(Detector):
    """YOLOE-26 text-prompted detection.

    Classes are configured before the run (ADR-3); runtime prompt changes are
    not part of E-1.
    """

    def __init__(self, model_path: Path, device: str | None = "0",
                 prompts: list[str] | None = None,
                 text_encoder: Path | None = None,
                 confidence: float = 0.35, iou: float = 0.5) -> None:
        super().__init__(model_path, device)
        self.prompts = prompts or []
        self.text_encoder = text_encoder
        self.confidence = confidence
        self.iou = iou
        self._model = None

    def load(self) -> None:
        if self._model is not None:
            return
        from ultralytics import YOLO  # deferred: heavy import

        self._model = YOLO(str(self.model_path))
        self._model.to(self.device)
        # Point the text encoder at the pre-placed asset (E-1-T4: no first-use
        # downloads). Ultralytics' build_text_model hard-codes the mobileclip2
        # weight filename, so the yaml/attribute override is not honoured; the
        # reliable lever is SETTINGS["weights_dir"], which attempt_download_asset
        # checks before downloading. The pre-placed file must live there.
        if self.text_encoder is not None:
            te = Path(self.text_encoder)
            if not te.exists():
                raise FileNotFoundError(f"text encoder asset not found: {te}")
            from ultralytics.utils import SETTINGS, WEIGHTS_DIR

            dest = WEIGHTS_DIR / te.name
            if not dest.exists():
                import shutil

                WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(te, dest)
            SETTINGS["weights_dir"] = str(WEIGHTS_DIR)
        # YOLOE-26 binds text prompts by fusing embeddings into the model before
        # inference (ADR-3: classes configured before the run). This generates
        # the MobileCLIP text-encoder pass once, up front.
        if self.prompts:
            self._model.set_classes(self.prompts)

    def detect(self, frame_index: int, timestamp_s: float, bgr_frame) -> FrameResult:
        assert self._model is not None, "call load() first"
        results = self._model.predict(
            bgr_frame,
            conf=self.confidence,
            iou=self.iou,
            verbose=False,
        )
        detections: list[Detection] = []
        if results:
            res = results[0]
            h, w = bgr_frame.shape[:2]
            boxes = res.boxes
            for i in range(len(boxes)):
                x1, y1, x2, y2 = (float(v) for v in boxes.xyxy[i].tolist())
                cls_id = int(boxes.cls[i])
                conf = float(boxes.conf[i])
                label = res.names.get(cls_id, str(cls_id))
                detections.append(
                    Detection(
                        class_id=cls_id,
                        label=label,
                        confidence=conf,
                        box=(x1 / w, y1 / h, x2 / w, y2 / h),
                    )
                )
        return FrameResult(frame_index, timestamp_s, bgr_frame.shape[1],
                           bgr_frame.shape[0], detections)
