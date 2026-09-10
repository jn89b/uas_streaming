#!/usr/bin/env python3
"""
detect_stream.py  (Hailo-8, low-latency ingest, per-stage timing, on-chip or raw-head post-processing)

    MediaMTX /eo (H.265) --> GStreamer: rtspsrc -> hw decode -> scale -> raw BGR over pipe
        --> [new frames only] letterbox --> Hailo-8 --> decode boxes --> draw
        --> RtspPublisher (fixed fps, libx264) --> MediaMTX /detections --> laptop

Two kinds of HEF are supported and detected automatically at startup:

  * single output in HAILO NMS format (e.g. /usr/share/hailo-models/yolov8s_h8.hef):
    boxes come back finished, one array per class.
  * several raw head outputs (a model compiled without the NMS post-process):
    one box tensor + one class tensor per scale. Box tensors may be 64-channel
    (DFL, YOLOv8 style) or 4-channel (direct ltrb distances, YOLO26 style).
    Decoding + NMS is done here in numpy/OpenCV.

Timing stages printed every --report seconds:
    ingest / age / letterbox / hailo / post / write   (see earlier version's notes)

Run on the Pi:
    python3 detect_stream.py --hef ~/model.hef --out-size 1280x720 --labels boat
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS",
                      "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

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


# ---------------------------------------------------------------------------
# Timing collector
# ---------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self._d = defaultdict(lambda: [0.0, 0, 0.0])
        self._c = defaultdict(int)
        self.t0 = time.perf_counter()

    def add(self, name, seconds):
        with self.lock:
            d = self._d[name]
            d[0] += seconds
            d[1] += 1
            if seconds > d[2]:
                d[2] = seconds

    def count(self, name, n=1):
        with self.lock:
            self._c[name] += n

    def report(self):
        with self.lock:
            dt = time.perf_counter() - self.t0
            lines = []
            for name in ("ingest", "age", "letterbox", "hailo", "post", "write"):
                s, n, mx = self._d.get(name, [0.0, 0, 0.0])
                if n:
                    lines.append(f"  {name:<10} avg {1000*s/n:6.1f} ms  max {1000*mx:6.1f} ms  n={n}")
            fps = {k: v / dt for k, v in self._c.items()}
            lines.append("  " + "  ".join(f"{k} {v:5.1f}/s" for k, v in sorted(fps.items())))
            self._d.clear()
            self._c.clear()
            self.t0 = time.perf_counter()
        return "\n".join(lines)


STATS = Stats()


# ---------------------------------------------------------------------------
# 1. Frame sources
# ---------------------------------------------------------------------------
def _gst_has(element: str) -> bool:
    try:
        return subprocess.run(["gst-inspect-1.0", element],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except FileNotFoundError:
        return False


class GstSource:
    """Hardware-decoded ingest handing raw BGR frames to Python over a pipe.

    frontend="ffmpeg" (default): ffmpeg's RTSP client pulls the stream and writes
    the compressed H.265 to GStreamer as fast as it arrives. No RTP jitter buffer,
    no timestamp-scheduled release, so nothing upstream can hoard frames. Over TCP
    the transport is in-order and lossless already, so a jitter buffer adds only
    the risk we hit: if the camera's RTP clock runs slower than packets arrive,
    rtspsrc queues the difference forever (or, with drop-on-latency, tears frames).

    frontend="rtspsrc": the previous GStreamer-native pull, kept for comparison.

    In both cases leaky queues after the decoder drop whole decoded frames if the
    Python side is slow, so latency stays flat."""

    def __init__(self, url, width, height, decoder, latency_ms, frontend="ffmpeg",
                 scale_first=True, nthreads=4):
        self.w, self.h = width, height
        self.nbytes = width * height * 3
        self.frame = None
        self.seq = 0
        self.t_arrival = 0.0
        self.lock = threading.Lock()
        self.first = threading.Event()
        self.ff = None

        if scale_first:
            # Scale the decoder's NV12 output down first, then convert the small
            # frame to BGR: ~4x less conversion work than converting at 1080p.
            mid = ["videoscale", f"n-threads={nthreads}", "!",
                   f"video/x-raw,width={width},height={height}", "!",
                   "videoconvert", f"n-threads={nthreads}", "!",
                   "video/x-raw,format=BGR"]
        else:
            mid = ["videoconvert", "!", "videoscale", "!",
                   f"video/x-raw,format=BGR,width={width},height={height}"]
        tail = [
            decoder, "!",
            "queue", "leaky=downstream", "max-size-buffers=1", "!",
            *mid, "!",
            "queue", "leaky=downstream", "max-size-buffers=1", "!",
            "fdsink", "fd=1", "sync=false",
        ]
        if frontend == "ffmpeg":
            self.ff = subprocess.Popen(
                [
                    "ffmpeg", "-loglevel", "error", "-nostdin",
                    "-rtsp_transport", "tcp", "-fflags", "nobuffer", "-flags", "low_delay",
                    "-probesize", "32", "-analyzeduration", "0", "-max_delay", "0",
                    "-i", url, "-an", "-c:v", "copy",
                    "-f", "hevc", "-flush_packets", "1", "-",
                ],
                stdout=subprocess.PIPE, bufsize=0,
            )
            pipeline = ["fdsrc", "fd=0", "!", "h265parse", "!", *tail]
            self.proc = subprocess.Popen(["gst-launch-1.0", "-q", *pipeline],
                                         stdin=self.ff.stdout, stdout=subprocess.PIPE, bufsize=0)
            self.ff.stdout.close()          # gst-launch owns the read end now
            _widen_pipe(self.proc.stdout)
        else:
            pipeline = [
                "rtspsrc", f"location={url}", "protocols=tcp", f"latency={latency_ms}", "!",
                "rtph265depay", "!", "h265parse", "!", *tail,
            ]
            self.proc = subprocess.Popen(["gst-launch-1.0", "-q", *pipeline],
                                         stdout=subprocess.PIPE, bufsize=0)
            _widen_pipe(self.proc.stdout)
        threading.Thread(target=self._loop, daemon=True).start()

    def _read_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def _loop(self):
        last = None
        while True:
            data = self._read_exact(self.nbytes)
            if data is None:
                print("[source] GStreamer pipeline ended", file=sys.stderr)
                return
            now = time.perf_counter()
            if last is not None:
                STATS.add("ingest", now - last)
            last = now
            STATS.count("in")
            frame = np.frombuffer(data, np.uint8).reshape(self.h, self.w, 3)
            with self.lock:
                self.frame = frame
                self.seq += 1
                self.t_arrival = now
            self.first.set()

    def ready(self, timeout):
        return self.first.wait(timeout)

    def alive(self):
        if self.ff is not None and self.ff.poll() is not None:
            return False
        return self.proc.poll() is None

    def get(self, after_seq):
        with self.lock:
            if self.frame is None or self.seq == after_seq:
                return None
            return self.seq, self.t_arrival, self.frame.copy()

    def close(self):
        self.proc.kill()
        if self.ff is not None:
            self.ff.kill()


def _widen_pipe(f, size=1 << 20):
    """Grow a pipe's kernel buffer (default 64 KB) so a multi-MB raw frame crosses
    it in a few reads instead of dozens. Best effort."""
    try:
        import fcntl
        fcntl.fcntl(f.fileno(), getattr(fcntl, "F_SETPIPE_SZ", 1031), size)
    except Exception:
        pass


class FfmpegSource:
    """ffmpeg does the whole ingest in one process: RTSP pull, H.265 software decode
    (frame-threaded across all cores), frame-rate reduction AFTER decode, scale,
    BGR conversion, raw frames to a pipe. No GStreamer, no V4L2 hardware decoder.

    Use where v4l2slh265dec is unavailable or hangs. A Pi 5 software-decodes 720p
    HEVC at well over 100 fps, so 60 fps in costs roughly 1.5 cores; the fps
    filter then hands Python only --ingest-fps frames per second."""

    def __init__(self, url, width, height, fps_out):
        self.w, self.h = width, height
        self.nbytes = width * height * 3
        self.frame = None
        self.seq = 0
        self.t_arrival = 0.0
        self.lock = threading.Lock()
        self.first = threading.Event()
        vf = ([f"fps={fps_out}"] if fps_out else []) + [f"scale={width}:{height}:flags=bilinear"]
        self.proc = subprocess.Popen(
            [
                "ffmpeg", "-loglevel", "error", "-nostdin",
                "-rtsp_transport", "tcp", "-fflags", "nobuffer", "-flags", "low_delay",
                "-probesize", "32", "-analyzeduration", "0", "-max_delay", "0",
                "-i", url, "-an",
                "-vf", ",".join(vf), "-pix_fmt", "bgr24", "-f", "rawvideo", "-",
            ],
            stdout=subprocess.PIPE, bufsize=0,
        )
        _widen_pipe(self.proc.stdout)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        last = None
        out = self.proc.stdout
        while True:
            buf = bytearray(self.nbytes)          # fresh buffer per frame: no reuse races
            mv = memoryview(buf)
            got = 0
            while got < self.nbytes:
                n = out.readinto(mv[got:])
                if not n:
                    print("[source] ffmpeg decode pipeline ended", file=sys.stderr)
                    return
                got += n
            now = time.perf_counter()
            if last is not None:
                STATS.add("ingest", now - last)
            last = now
            STATS.count("in")
            frame = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
            with self.lock:
                self.frame = frame
                self.seq += 1
                self.t_arrival = now
            self.first.set()

    def ready(self, timeout):
        return self.first.wait(timeout)

    def alive(self):
        return self.proc.poll() is None

    def get(self, after_seq):
        with self.lock:
            if self.frame is None or self.seq == after_seq:
                return None
            return self.seq, self.t_arrival, self.frame.copy()

    def close(self):
        self.proc.kill()


class CvSource:
    """Fallback: OpenCV + FFmpeg. Higher latency than GstSource."""

    def __init__(self, url, width, height):
        self.size = (width, height)
        self.cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not self.cap.isOpened():
            sys.exit(f"[source] could not open {url}")
        self.frame = None
        self.seq = 0
        self.t_arrival = 0.0
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        last = None
        while True:
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            now = time.perf_counter()
            if last is not None:
                STATS.add("ingest", now - last)
            last = now
            STATS.count("in")
            if (frame.shape[1], frame.shape[0]) != self.size:
                frame = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
            with self.lock:
                self.frame = frame
                self.seq += 1
                self.t_arrival = now

    def alive(self):
        return True

    def get(self, after_seq):
        with self.lock:
            if self.frame is None or self.seq == after_seq:
                return None
            return self.seq, self.t_arrival, self.frame.copy()


def open_source(url, width, height, decoder, latency_ms, frontend, ingest_fps):
    order = {
        "auto": ["v4l2slh265dec", "ffmpeg-sw", "avdec_h265"],
        "v4l2slh265dec": ["v4l2slh265dec"],
        "ffmpeg-sw": ["ffmpeg-sw"],
        "avdec_h265": ["avdec_h265"],
        "opencv": [],
    }[decoder]
    for dec in order:
        if dec == "ffmpeg-sw":
            src = FfmpegSource(url, width, height, ingest_fps)
            if src.ready(timeout=15):
                print(f"[source] ffmpeg software decode -> fps={ingest_fps or 'source'} -> {width}x{height} BGR")
                return src
            print("[source] ffmpeg software decode produced no frames in 15 s, trying next")
            src.close()
            continue
        if not _gst_has(dec):
            print(f"[source] {dec} not available")
            continue
        for scale_first in (True, False):
            src = GstSource(url, width, height, dec, latency_ms, frontend, scale_first=scale_first)
            if src.ready(timeout=15):
                front = "ffmpeg rtsp client -> " if frontend == "ffmpeg" else f"rtspsrc (jitter buffer {latency_ms} ms) -> "
                order_txt = "scale then convert (threaded)" if scale_first else "convert then scale"
                print(f"[source] {front}{dec} -> {order_txt} -> {width}x{height} BGR")
                return src
            print(f"[source] {dec} ({'scale-first' if scale_first else 'convert-first'}) produced no frames in 15 s, trying next")
            src.close()
    print("[source] OpenCV/FFmpeg fallback (expect ~1 s more latency)")
    return CvSource(url, width, height)


# ---------------------------------------------------------------------------
# 2. Post-processing for raw YOLO heads (no NMS in the HEF)
# ---------------------------------------------------------------------------
def _sigmoid(x):
    with np.errstate(over="ignore"):
        return 1.0 / (1.0 + np.exp(-x))


class RawYoloHead:
    """Decodes raw YOLOv8/YOLO26-style head outputs.

    Expects, per scale, one box tensor (H, W, 64) [DFL] or (H, W, 4) [direct ltrb]
    and one class tensor (H, W, num_classes), NHWC. Scales are matched by spatial
    size; stride = input_height / H. Boxes are returned in model-input pixels."""

    def __init__(self, shapes, in_w, in_h, scores_mode):
        by_hw = defaultdict(list)
        for name, shp in shapes.items():
            if len(shp) != 3:
                sys.exit(f"[hailo] unexpected output shape for {name}: {shp} (expected H,W,C)")
            h, w, c = shp
            by_hw[(h, w)].append((name, c))

        self.levels = []
        nc = None
        for (h, w), members in sorted(by_hw.items(), key=lambda kv: -(kv[0][0] * kv[0][1])):
            if len(members) != 2:
                sys.exit(f"[hailo] expected one box and one class tensor at {h}x{w}, got {members}")
            box_candidates = [m for m in members if m[1] in (4, 64)]
            if len(box_candidates) != 1:
                sys.exit(f"[hailo] cannot tell box from class tensor at {h}x{w}: {members}")
            box_name, box_c = box_candidates[0]
            cls_name, cls_c = next(m for m in members if m[0] != box_name)
            nc = cls_c if nc is None else nc
            if cls_c != nc:
                sys.exit("[hailo] class channel count differs between scales")
            stride = in_h / h
            if abs(in_w / w - stride) > 1e-3:
                print(f"[hailo] warning: non-square stride at {h}x{w} ({in_h/h:.2f} vs {in_w/w:.2f})")
            ys, xs = np.mgrid[0:h, 0:w]
            anchors = np.stack([xs.ravel() + 0.5, ys.ravel() + 0.5], axis=1).astype(np.float32)
            self.levels.append(dict(box=box_name, cls=cls_name, stride=stride, anchors=anchors,
                                    dfl=(box_c == 64), hw=(h, w)))
            print(f"[hailo]   scale {h}x{w}  stride {stride:.0f}  box={box_name} ({box_c}ch"
                  f"{', DFL' if box_c == 64 else ', direct'})  cls={cls_name} ({cls_c} classes)")
        self.nc = nc
        self.scores_mode = scores_mode          # "auto" | "logits" | "probs"
        self.dfl_idx = np.arange(16, dtype=np.float32)

    def decode(self, outs, conf, iou):
        boxes, scores, classes = [], [], []
        for lv in self.levels:
            cls = outs[lv["cls"]].reshape(-1, self.nc)
            if self.scores_mode == "auto":
                self.scores_mode = "probs" if (cls.min() >= 0.0 and cls.max() <= 1.0) else "logits"
                print(f"[hailo] class outputs look like {self.scores_mode}"
                      + (" (applying sigmoid)" if self.scores_mode == "logits" else ""))
            best = cls.argmax(axis=1)
            best_s = cls[np.arange(len(best)), best]
            # Threshold in logit space and sigmoid only the survivors: no exp()
            # over every anchor, so 'post' shrinks and the overflow warning goes away.
            thr = float(np.log(conf / (1.0 - conf))) if self.scores_mode == "logits" else conf
            keep = best_s >= thr
            if not keep.any():
                continue
            best_s = _sigmoid(best_s[keep]) if self.scores_mode == "logits" else best_s[keep]
            best = best[keep]
            box = outs[lv["box"]]
            box = box.reshape(-1, box.shape[-1])[keep]
            if lv["dfl"]:
                box = box.reshape(-1, 4, 16)
                box = box - box.max(axis=2, keepdims=True)
                e = np.exp(box)
                dist = ((e / e.sum(axis=2, keepdims=True)) * self.dfl_idx).sum(axis=2)
            else:
                dist = box
            a = lv["anchors"][keep]
            s = lv["stride"]
            boxes.append(np.stack([(a[:, 0] - dist[:, 0]) * s, (a[:, 1] - dist[:, 1]) * s,
                                   (a[:, 0] + dist[:, 2]) * s, (a[:, 1] + dist[:, 3]) * s], axis=1))
            scores.append(best_s)
            classes.append(best)
        if not boxes:
            return []
        boxes = np.concatenate(boxes)
        scores = np.concatenate(scores)
        classes = np.concatenate(classes)
        # NMS: required for one-to-many heads, harmless for NMS-free (YOLO26) heads.
        xywh = np.stack([boxes[:, 0], boxes[:, 1], boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1]], 1)
        idx = cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), conf, iou)
        idx = np.array(idx).reshape(-1)
        return [(boxes[i, 0], boxes[i, 1], boxes[i, 2], boxes[i, 3], float(scores[i]), int(classes[i]))
                for i in idx]


# ---------------------------------------------------------------------------
# 3. Detector wrapper: Hailo-8 via the HailoRT InferModel API
# ---------------------------------------------------------------------------
class HailoDetector:
    def __init__(self, hef_path, conf, iou, labels, scores_mode):
        from hailo_platform import HEF, VDevice, HailoSchedulingAlgorithm, FormatType

        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        self.vdevice = VDevice(params)
        self.infer_model = self.vdevice.create_infer_model(hef_path)
        self.infer_model.set_batch_size(1)
        self.infer_model.input().set_format_type(FormatType.UINT8)

        hef = HEF(hef_path)
        self.in_h, self.in_w, _ = hef.get_input_vstream_infos()[0].shape
        out_infos = hef.get_output_vstream_infos()
        self.out_names = [i.name for i in out_infos]
        for n in self.out_names:
            self.infer_model.output(n).set_format_type(FormatType.FLOAT32)
        self.out_shapes = {n: tuple(self.infer_model.output(n).shape) for n in self.out_names}

        print(f"[hailo] {os.path.basename(hef_path)} input {self.in_w}x{self.in_h}, {len(self.out_names)} output(s)")
        for i in out_infos:
            print(f"[hailo]   {i.name}: shape {self.out_shapes[i.name]} order {i.format.order}")

        self.nms_on_chip = len(self.out_names) == 1 and "NMS" in str(out_infos[0].format.order).upper()
        if self.nms_on_chip:
            try:
                nc = out_infos[0].nms_shape.number_of_classes
            except Exception:
                nc = 80
            self.head = None
            print(f"[hailo] on-chip NMS output, {nc} classes")
        else:
            self.head = RawYoloHead(self.out_shapes, self.in_w, self.in_h, scores_mode)
            nc = self.head.nc
            print(f"[hailo] raw head outputs, decoding in Python, {nc} classes")

        if labels:
            self.labels = labels
        elif nc == 80:
            self.labels = COCO_LABELS
        else:
            self.labels = [f"class{i}" for i in range(nc)]
        if len(self.labels) < nc:
            self.labels = self.labels + [f"class{i}" for i in range(len(self.labels), nc)]

        self.conf, self.iou = conf, iou
        self.configured = self.infer_model.configure()

    def letterbox(self, frame_bgr):
        h, w = frame_bgr.shape[:2]
        scale = min(self.in_w / w, self.in_h / h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        resized = cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.in_h, self.in_w, 3), 114, dtype=np.uint8)
        dx, dy = (self.in_w - nw) // 2, (self.in_h - nh) // 2
        canvas[dy:dy + nh, dx:dx + nw] = resized
        return cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB), scale, dx, dy

    def run(self, rgb):
        bindings = self.configured.create_bindings(
            output_buffers={n: np.empty(self.out_shapes[n], dtype=np.float32) for n in self.out_names})
        bindings.input().set_buffer(np.ascontiguousarray(rgb))
        self.configured.run([bindings], 1000)
        return {n: bindings.output(n).get_buffer() for n in self.out_names}

    def decode(self, outs):
        """-> list of (x1, y1, x2, y2, score, class_idx) in model-input pixels."""
        if self.nms_on_chip:
            dets = []
            for cls_idx, rows in enumerate(outs[self.out_names[0]]):
                for ymin, xmin, ymax, xmax, score in rows:
                    if score >= self.conf:
                        dets.append((xmin * self.in_w, ymin * self.in_h, xmax * self.in_w, ymax * self.in_h,
                                     float(score), cls_idx))
            return dets
        return self.head.decode(outs, self.conf, self.iou)

    def to_source(self, dets, scale, dx, dy, w, h):
        """Undo the letterbox -> (x1, y1, x2, y2, conf, label) in source-frame pixels."""
        out = []
        for x1, y1, x2, y2, score, cls_idx in dets:
            out.append((int(max(0, (x1 - dx) / scale)), int(max(0, (y1 - dy) / scale)),
                        int(min(w - 1, (x2 - dx) / scale)), int(min(h - 1, (y2 - dy) / scale)),
                        score, self.labels[cls_idx]))
        return out


def draw(frame, detections, stamp):
    for x1, y1, x2, y2, conf, label in detections:
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(frame, f"{label} {conf:.2f}", (x1, max(0, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    if stamp:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        cv2.putText(frame, ts, (8, frame.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4)
        cv2.putText(frame, ts, (8, frame.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    return frame


# ---------------------------------------------------------------------------
# 4. Publisher: fixed-rate H.264 into MediaMTX
# ---------------------------------------------------------------------------
class RtspPublisher:
    def __init__(self, url, width, height, fps, bitrate, bufsize, nice_extra=0):
        self.size = (width, height)
        self.fps = fps
        self.frame = None
        self.frame_seq = 0
        self.lock = threading.Lock()
        prefix = ["nice", "-n", str(nice_extra)] if nice_extra > 0 else []
        self.proc = subprocess.Popen(
            [
                *prefix, "ffmpeg", "-loglevel", "error", "-nostdin",
                "-fflags", "nobuffer", "-probesize", "32", "-analyzeduration", "0",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bufsize,
                "-g", str(fps), "-pix_fmt", "yuv420p",
                "-f", "rtsp", "-rtsp_transport", "tcp", url,
            ],
            stdin=subprocess.PIPE,
        )
        threading.Thread(target=self._loop, daemon=True).start()

    def alive(self):
        return self.proc.poll() is None

    def push(self, frame):
        # Just hand over the reference; resize/serialise happen in the publisher
        # thread so they don't sit on the inference loop.
        with self.lock:
            self.frame = frame
            self.frame_seq += 1

    def _loop(self):
        period = 1.0 / self.fps
        last_seq = -1
        payload = None
        while self.proc.poll() is None:
            t0 = time.perf_counter()
            with self.lock:
                frame, seq = self.frame, self.frame_seq
            if frame is not None:
                if seq != last_seq:
                    if (frame.shape[1], frame.shape[0]) != self.size:
                        frame = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
                    payload = frame.tobytes()
                else:
                    STATS.count("out_dup")
                last_seq = seq
                tw = time.perf_counter()
                try:
                    self.proc.stdin.write(payload)
                except BrokenPipeError:
                    break
                STATS.add("write", time.perf_counter() - tw)
                STATS.count("out")
            time.sleep(max(0.0, period - (time.perf_counter() - t0)))


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="rtsp://127.0.0.1:8554/eo")
    ap.add_argument("--publish", default="rtsp://127.0.0.1:8554/detections")
    ap.add_argument("--hef", required=True)
    ap.add_argument("--conf", type=float, default=0.4)
    ap.add_argument("--iou", type=float, default=0.5, help="NMS IoU threshold (raw-head models only)")
    ap.add_argument("--labels", default=None,
                    help="comma-separated class names, or path to a text file with one name per line")
    ap.add_argument("--scores", default="auto", choices=["auto", "logits", "probs"],
                    help="whether raw class outputs need a sigmoid (auto-detected by default)")
    ap.add_argument("--out-size", default="960x540",
                    help="working size the model sees; use >= the HEF input width, e.g. 1280x720 for a 1280x736 model")
    ap.add_argument("--publish-size", default=None,
                    help="size of the published stream if different from --out-size, e.g. 960x540")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--bitrate", default="2M")
    ap.add_argument("--bufsize", default="500k")
    ap.add_argument("--ingest", default="ffmpeg", choices=["ffmpeg", "rtspsrc"],
                    help="how the compressed stream is pulled (ffmpeg = no jitter buffer, recommended)")
    ap.add_argument("--latency", type=int, default=100, help="rtspsrc jitter buffer in ms (--ingest rtspsrc only)")
    ap.add_argument("--decoder", default="auto",
                    choices=["auto", "v4l2slh265dec", "ffmpeg-sw", "avdec_h265", "opencv"],
                    help="v4l2slh265dec = Pi 5 hardware via GStreamer; ffmpeg-sw = software in ffmpeg, "
                         "no GStreamer (use where the hardware path hangs)")
    ap.add_argument("--ingest-fps", type=int, default=30,
                    help="frame rate handed to Python after decode (ffmpeg-sw only); 0 = camera rate")
    ap.add_argument("--report", type=float, default=5.0)
    ap.add_argument("--no-stamp", action="store_true")
    ap.add_argument("--no-nice", action="store_true",
                    help="don't lower this process's and the encoder's CPU priority below the decode chain")
    args = ap.parse_args()

    work_w, work_h = (int(v) for v in args.out_size.lower().split("x"))
    pub_w, pub_h = (int(v) for v in (args.publish_size or args.out_size).lower().split("x"))
    stamp = not args.no_stamp

    labels = None
    if args.labels:
        if os.path.isfile(args.labels):
            with open(args.labels) as f:
                labels = [ln.strip() for ln in f if ln.strip()]
        else:
            labels = [s.strip() for s in args.labels.split(",") if s.strip()]

    detector = HailoDetector(args.hef, args.conf, args.iou, labels, args.scores)
    if work_w < detector.in_w or work_h < detector.in_h - 32:
        print(f"[hailo] note: working size {work_w}x{work_h} is smaller than the model input "
              f"{detector.in_w}x{detector.in_h}; frames will be upscaled. Consider a larger --out-size.")
    source = open_source(args.source, work_w, work_h, args.decoder, args.latency, args.ingest, args.ingest_fps)
    if not args.no_nice:
        # The decode chain (already running, nice 0) must always win the CPU: if it
        # falls behind the camera's frame rate, latency piles up upstream where
        # nothing can drop frames. This process runs at nice 5 and x264 at nice 10.
        os.nice(5)
    publisher = RtspPublisher(args.publish, pub_w, pub_h, args.fps, args.bitrate, args.bufsize,
                              nice_extra=0 if args.no_nice else 5)
    print(f"[publish] {args.publish}  ({pub_w}x{pub_h} @ {args.fps} fps, {args.bitrate})")

    last_seq = 0
    last_report = time.perf_counter()
    last_dets = []
    while source.alive():
        got = source.get(last_seq)
        if got is None:
            time.sleep(0.002)
        else:
            last_seq, t_arrival, frame = got
            t = time.perf_counter()
            STATS.add("age", t - t_arrival)

            rgb, scale, dx, dy = detector.letterbox(frame)
            t1 = time.perf_counter()
            STATS.add("letterbox", t1 - t)

            outs = detector.run(rgb)
            t2 = time.perf_counter()
            STATS.add("hailo", t2 - t1)

            last_dets = detector.to_source(detector.decode(outs), scale, dx, dy, frame.shape[1], frame.shape[0])
            publisher.push(draw(frame, last_dets, stamp))
            STATS.add("post", time.perf_counter() - t2)
            STATS.count("processed")

        if not publisher.alive():
            sys.exit(f"[publish] ffmpeg exited with code {publisher.proc.returncode}; "
                     f"it could not publish to {args.publish} (its error is printed above)")

        if time.perf_counter() - last_report >= args.report:
            print(f"[timing] last {args.report:.0f} s, {len(last_dets)} detections in last frame\n{STATS.report()}")
            last_report = time.perf_counter()

    sys.exit("[source] stream ended")


if __name__ == "__main__":
    main()
