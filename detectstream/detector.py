"""Hailo inference and post-processing.

Two HEF flavours are supported and told apart from the HEF itself:

* one output in HAILO NMS format (on-chip NMS): boxes arrive finished, one
  array per class with rows [y_min, x_min, y_max, x_max, score] normalised
  to the model input;
* several raw head outputs (no NMS compiled in): one box tensor and one class
  tensor per scale, NHWC. Box tensors are 64-channel (DFL, YOLOv8 style) or
  4-channel (direct l/t/r/b distances in grid units, YOLO26 style). Decoding
  and NMS happen here in numpy/OpenCV.

Everything that does not need the accelerator (letterboxing, decoding, the
inverse mapping, label handling) is a plain function or a class with no Hailo
dependency, so it is unit-testable without hardware. HailoDetector wraps the
device and exposes preprocess / run / postprocess so the main loop can time
each step and tests can substitute a fake.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from typing import Optional, Sequence, Union

import cv2
import numpy as np

log = logging.getLogger("hailo")

COCO_LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]

LETTERBOX_FILL = 114              # grey used for padding, as in Ultralytics
DFL_BINS = 16                     # bins per side in a DFL box head (4 * 16 = 64 channels)
BOX_CHANNEL_COUNTS = (4, 4 * DFL_BINS)
SCORES_MODES = ("auto", "logits", "probs")


@dataclass(frozen=True)
class Detection:
    """A box in source-frame pixels with a human-readable label."""
    x1: int
    y1: int
    x2: int
    y2: int
    score: float
    label: str


@dataclass(frozen=True)
class RawDetection:
    """A box in model-input pixels, before mapping back to the source frame."""
    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    class_id: int


@dataclass(frozen=True)
class LetterboxGeometry:
    """How a source frame was fitted into the model input."""
    scale: float
    dx: int
    dy: int
    src_w: int
    src_h: int


# ---------------------------------------------------------------------------
# Geometry (no Hailo dependency)
# ---------------------------------------------------------------------------
def letterbox(frame_bgr: np.ndarray, in_w: int, in_h: int) -> tuple[np.ndarray, LetterboxGeometry]:
    """Scale to fit, pad to in_w x in_h with grey, convert BGR->RGB."""
    src_h, src_w = frame_bgr.shape[:2]
    scale = min(in_w / src_w, in_h / src_h)
    new_w, new_h = int(round(src_w * scale)), int(round(src_h * scale))
    resized = cv2.resize(frame_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((in_h, in_w, 3), LETTERBOX_FILL, dtype=np.uint8)
    dx, dy = (in_w - new_w) // 2, (in_h - new_h) // 2
    canvas[dy:dy + new_h, dx:dx + new_w] = resized
    return cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB), LetterboxGeometry(scale, dx, dy, src_w, src_h)


def to_source(raw: Sequence[RawDetection], geom: LetterboxGeometry, labels: Sequence[str]) -> list[Detection]:
    """Undo the letterbox: model-input pixels -> source-frame pixels, clipped to the frame."""
    out = []
    for r in raw:
        out.append(Detection(
            x1=int(max(0, (r.x1 - geom.dx) / geom.scale)),
            y1=int(max(0, (r.y1 - geom.dy) / geom.scale)),
            x2=int(min(geom.src_w - 1, (r.x2 - geom.dx) / geom.scale)),
            y2=int(min(geom.src_h - 1, (r.y2 - geom.dy) / geom.scale)),
            score=float(r.score),
            label=labels[r.class_id] if r.class_id < len(labels) else str(r.class_id),
        ))
    return out


# ---------------------------------------------------------------------------
# Output decoders (no Hailo dependency)
# ---------------------------------------------------------------------------
def sigmoid(x: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore"):
        return 1.0 / (1.0 + np.exp(-x))


def decode_nms_by_class(per_class: Sequence[np.ndarray], conf: float, in_w: int, in_h: int) -> list[RawDetection]:
    """Decode a HAILO NMS output: one (n, 5) array per class, rows normalised
    [y_min, x_min, y_max, x_max, score]."""
    out = []
    for class_id, rows in enumerate(per_class):
        for ymin, xmin, ymax, xmax, score in rows:
            if score >= conf:
                out.append(RawDetection(float(xmin) * in_w, float(ymin) * in_h,
                                        float(xmax) * in_w, float(ymax) * in_h, float(score), class_id))
    return out


class RawYoloHead:
    """Decoder for raw YOLOv8/YOLO26-style head outputs.

    Scales are paired by spatial size; stride = input_height / H. The box
    tensor at each scale is identified by its channel count (4 or 64), so a
    model with exactly 4 or 64 classes is ambiguous and rejected up front.

    scores_mode: "logits" (apply sigmoid), "probs" (already 0..1), or "auto"
    (decided on the first frame: raw logits for an empty scene sit well below
    zero, probabilities sit within 0..1).
    """

    def __init__(self, shapes: dict[str, tuple[int, ...]], in_w: int, in_h: int,
                 scores_mode: str = "auto") -> None:
        if scores_mode not in SCORES_MODES:
            raise ValueError(f"scores_mode must be one of {SCORES_MODES}, got {scores_mode!r}")
        self.scores_mode = scores_mode
        self._dfl_index = np.arange(DFL_BINS, dtype=np.float32)

        by_size: dict[tuple[int, int], list[tuple[str, int]]] = {}
        for name, shape in shapes.items():
            if len(shape) != 3:
                raise ValueError(f"output {name} has shape {shape}; expected (H, W, C)")
            h, w, c = shape
            by_size.setdefault((h, w), []).append((name, c))

        self.levels: list[dict] = []
        num_classes: Optional[int] = None
        for (h, w), members in sorted(by_size.items(), key=lambda kv: -(kv[0][0] * kv[0][1])):
            if len(members) != 2:
                raise ValueError(f"expected one box and one class tensor at {h}x{w}, got {members}")
            box_candidates = [m for m in members if m[1] in BOX_CHANNEL_COUNTS]
            if len(box_candidates) != 1:
                raise ValueError(f"cannot tell box from class tensor at {h}x{w}: {members} "
                                 f"(class count must not be one of {BOX_CHANNEL_COUNTS})")
            box_name, box_c = box_candidates[0]
            cls_name, cls_c = next(m for m in members if m[0] != box_name)
            if num_classes is None:
                num_classes = cls_c
            elif cls_c != num_classes:
                raise ValueError("class channel count differs between scales")
            stride = in_h / h
            if abs(in_w / w - stride) > 1e-3:
                log.warning("non-square stride at %dx%d (%.2f vs %.2f)", h, w, in_h / h, in_w / w)
            ys, xs = np.mgrid[0:h, 0:w]
            anchors = np.stack([xs.ravel() + 0.5, ys.ravel() + 0.5], axis=1).astype(np.float32)
            self.levels.append(dict(box=box_name, cls=cls_name, stride=stride, anchors=anchors,
                                    dfl=(box_c == 4 * DFL_BINS), size=(h, w)))
            log.info("  scale %dx%d  stride %.0f  box=%s (%dch, %s)  cls=%s (%d classes)",
                     h, w, stride, box_name, box_c, "DFL" if box_c == 4 * DFL_BINS else "direct",
                     cls_name, cls_c)
        assert num_classes is not None
        self.num_classes = num_classes

    def _resolve_scores_mode(self, cls: np.ndarray) -> None:
        self.scores_mode = "probs" if (cls.min() >= 0.0 and cls.max() <= 1.0) else "logits"
        log.info("class outputs look like %s%s", self.scores_mode,
                 " (applying sigmoid)" if self.scores_mode == "logits" else "")

    def decode(self, outputs: dict[str, np.ndarray], conf: float, iou: float) -> list[RawDetection]:
        all_boxes, all_scores, all_classes = [], [], []
        for lv in self.levels:
            cls = outputs[lv["cls"]].reshape(-1, self.num_classes)
            if self.scores_mode == "auto":
                self._resolve_scores_mode(cls)
            best = cls.argmax(axis=1)
            best_score = cls[np.arange(len(best)), best]
            # Threshold in logit space, then sigmoid only the survivors.
            threshold = float(np.log(conf / (1.0 - conf))) if self.scores_mode == "logits" else conf
            keep = best_score >= threshold
            if not keep.any():
                continue
            scores = sigmoid(best_score[keep]) if self.scores_mode == "logits" else best_score[keep]

            box = outputs[lv["box"]]
            box = box.reshape(-1, box.shape[-1])[keep]
            if lv["dfl"]:
                box = box.reshape(-1, 4, DFL_BINS)
                box = box - box.max(axis=2, keepdims=True)
                e = np.exp(box)
                dist = ((e / e.sum(axis=2, keepdims=True)) * self._dfl_index).sum(axis=2)
            else:
                dist = box
            a = lv["anchors"][keep]
            s = lv["stride"]
            all_boxes.append(np.stack([(a[:, 0] - dist[:, 0]) * s, (a[:, 1] - dist[:, 1]) * s,
                                       (a[:, 0] + dist[:, 2]) * s, (a[:, 1] + dist[:, 3]) * s], axis=1))
            all_scores.append(scores)
            all_classes.append(best[keep])

        if not all_boxes:
            return []
        boxes = np.concatenate(all_boxes)
        scores = np.concatenate(all_scores)
        classes = np.concatenate(all_classes)
        # NMS is required for one-to-many heads and harmless for NMS-free ones.
        xywh = np.stack([boxes[:, 0], boxes[:, 1], boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1]], axis=1)
        kept = np.asarray(cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), conf, iou)).reshape(-1)
        return [RawDetection(float(boxes[i, 0]), float(boxes[i, 1]), float(boxes[i, 2]), float(boxes[i, 3]),
                             float(scores[i]), int(classes[i])) for i in kept]


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
LabelSpec = Union[None, str, Sequence[str]]


def resolve_labels(spec: LabelSpec, num_classes: int) -> list[str]:
    """spec may be None, a comma-separated string, a path to a file with one
    name per line, or a sequence. Missing names become classN; an 80-class
    model with no spec gets the COCO names."""
    if spec is None or spec == "":
        labels = list(COCO_LABELS) if num_classes == len(COCO_LABELS) else []
    elif isinstance(spec, str):
        if os.path.isfile(spec):
            with open(spec, encoding="utf-8") as f:
                labels = [line.strip() for line in f if line.strip()]
        else:
            labels = [s.strip() for s in spec.split(",") if s.strip()]
    else:
        labels = list(spec)
    labels += [f"class{i}" for i in range(len(labels), num_classes)]
    return labels


# ---------------------------------------------------------------------------
# The accelerator wrapper
# ---------------------------------------------------------------------------
class HailoDetector:
    """Runs a HEF on the Hailo device.

    preprocess(frame) -> (rgb, geometry); run(rgb) -> raw outputs;
    postprocess(outputs, geometry) -> Detections in source-frame pixels.

    Owns the VDevice and the configured model; call close() (or use as a
    context manager) to release the accelerator for other processes.
    """

    def __init__(self, hef_path: str, conf: float = 0.4, iou: float = 0.5,
                 labels: LabelSpec = None, scores_mode: str = "auto", timeout_ms: int = 1000) -> None:
        from hailo_platform import HEF, FormatType, HailoSchedulingAlgorithm, VDevice  # noqa: PLC0415

        self.conf, self.iou, self.timeout_ms = conf, iou, timeout_ms
        self._stack = contextlib.ExitStack()

        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        self._vdevice = VDevice(params)
        self._stack.callback(self._release_vdevice)
        self._infer_model = self._vdevice.create_infer_model(hef_path)
        self._infer_model.set_batch_size(1)
        self._infer_model.input().set_format_type(FormatType.UINT8)

        hef = HEF(hef_path)
        self.in_h, self.in_w, _ = hef.get_input_vstream_infos()[0].shape
        out_infos = hef.get_output_vstream_infos()
        self._out_names = [i.name for i in out_infos]
        for name in self._out_names:
            self._infer_model.output(name).set_format_type(FormatType.FLOAT32)
        self._out_shapes = {n: tuple(self._infer_model.output(n).shape) for n in self._out_names}

        log.info("%s: input %dx%d, %d output(s)", os.path.basename(hef_path), self.in_w, self.in_h, len(out_infos))
        for info in out_infos:
            log.info("  %s: shape %s order %s", info.name, self._out_shapes[info.name], info.format.order)

        self._on_chip_nms = len(out_infos) == 1 and "NMS" in str(out_infos[0].format.order).upper()
        if self._on_chip_nms:
            try:
                num_classes = int(out_infos[0].nms_shape.number_of_classes)
            except AttributeError:
                num_classes = len(COCO_LABELS)
            self._head = None
            log.info("on-chip NMS output, %d classes", num_classes)
        else:
            self._head = RawYoloHead(self._out_shapes, self.in_w, self.in_h, scores_mode)
            num_classes = self._head.num_classes
            log.info("raw head outputs, decoding on the CPU, %d classes", num_classes)

        self.num_classes = num_classes
        self.labels = resolve_labels(labels, num_classes)
        self._configured = self._stack.enter_context(self._infer_model.configure())

    def _release_vdevice(self) -> None:
        release = getattr(self._vdevice, "release", None)
        if callable(release):
            release()

    def close(self) -> None:
        self._stack.close()

    def __enter__(self) -> "HailoDetector":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the three steps the main loop times separately --------------------------
    def preprocess(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, LetterboxGeometry]:
        return letterbox(frame_bgr, self.in_w, self.in_h)

    def run(self, rgb: np.ndarray) -> dict[str, np.ndarray]:
        bindings = self._configured.create_bindings(
            output_buffers={n: np.empty(self._out_shapes[n], dtype=np.float32) for n in self._out_names})
        bindings.input().set_buffer(np.ascontiguousarray(rgb))
        self._configured.run([bindings], self.timeout_ms)
        return {n: bindings.output(n).get_buffer() for n in self._out_names}

    def postprocess(self, outputs: dict[str, np.ndarray], geom: LetterboxGeometry) -> list[Detection]:
        if self._on_chip_nms:
            raw = decode_nms_by_class(outputs[self._out_names[0]], self.conf, self.in_w, self.in_h)
        else:
            raw = self._head.decode(outputs, self.conf, self.iou)
        return to_source(raw, geom, self.labels)
