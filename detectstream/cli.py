"""Arguments, wiring, logging and shutdown for the detection stream."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import Optional, Sequence

from .detector import SCORES_MODES, HailoDetector
from .ingest import FfmpegSource
from .overlay import draw
from .publisher import CODEC_ARGS, RtspPublisher
from .stats import Stats

log = logging.getLogger("main")

EXIT_OK = 0                # stopped by a signal
EXIT_SOURCE_ENDED = 1      # camera stream ended: non-zero so systemd restarts us
EXIT_PUBLISHER_DIED = 2    # encoder / MediaMTX publish failed
EXIT_STARTUP = 3           # could not get a first frame

MIN_DIMENSION = 64
STARTUP_TIMEOUT_S = 20.0
LOOP_WAIT_S = 0.25
MAIN_NICE = 5              # this process relative to the decoder (nice 0)
ENCODER_NICE_EXTRA = 5     # encoder relative to this process


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
def parse_size(text: str) -> tuple[int, int]:
    """'1280x720' -> (1280, 720). Both even (x264 needs it) and at least MIN_DIMENSION."""
    try:
        w_str, h_str = text.lower().split("x")
        w, h = int(w_str), int(h_str)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"size must look like 1280x720, got {text!r}") from exc
    if w < MIN_DIMENSION or h < MIN_DIMENSION or w % 2 or h % 2:
        raise argparse.ArgumentTypeError(f"size must be even and at least {MIN_DIMENSION}, got {text!r}")
    return w, h


def _unit_interval(text: str) -> float:
    value = float(text)
    if not 0.0 < value < 1.0:
        raise argparse.ArgumentTypeError(f"must be between 0 and 1 (exclusive), got {text}")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="detect_stream",
        description="Run a Hailo detector on a live RTSP stream and republish the annotated video.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--hef", required=True, help="compiled model (.hef)")
    p.add_argument("--source", default="rtsp://127.0.0.1:8554/eo", help="input stream (MediaMTX proxy of the camera)")
    p.add_argument("--publish", default="rtsp://127.0.0.1:8554/detections", help="MediaMTX path to publish to")

    model = p.add_argument_group("model")
    model.add_argument("--conf", type=_unit_interval, default=0.4, help="score threshold")
    model.add_argument("--iou", type=_unit_interval, default=0.5, help="NMS IoU threshold (raw-head models)")
    model.add_argument("--labels", default=None, help="comma-separated class names, or a file with one per line")
    model.add_argument("--scores", default="auto", choices=SCORES_MODES, help="whether raw class outputs need a sigmoid")

    video = p.add_argument_group("video")
    video.add_argument("--work-size", type=parse_size, default=(1280, 720),
                       help="frame size the model sees; at least the HEF input width (1280x720 for a 1280x736 model)")
    video.add_argument("--publish-size", type=parse_size, default=None, help="published stream size (default: work size)")
    video.add_argument("--ingest-fps", type=int, default=30, help="frames per second handed to the model after decode (0 = source rate)")
    video.add_argument("--fps", type=int, default=30, help="published frame rate")
    video.add_argument("--bitrate", default="2M", help="published bitrate")
    video.add_argument("--bufsize", default="100k", help="encoder rate-control buffer; smaller = lower latency")
    video.add_argument("--codec", default="h264", choices=sorted(CODEC_ARGS),
                       help="published codec; h265 is smaller but costs several times the encoding CPU")
    video.add_argument("--hwaccel", default=None, help="ffmpeg -hwaccel for decode (experimental; default software)")
    video.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable")
    video.add_argument("--no-stamp", action="store_true", help="do not burn the wall-clock time into the frame")
    video.add_argument("--no-crosshair", action="store_true", help="do not draw the + at the frame centre")

    run = p.add_argument_group("runtime")
    run.add_argument("--report", type=float, default=5.0, help="seconds between timing reports")
    run.add_argument("--no-nice", action="store_true", help="keep this process and the encoder at normal CPU priority")
    run.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    # Accepted so older service units keep starting: --out-size is an alias, the others are ignored.
    p.add_argument("--out-size", dest="work_size", type=parse_size, help=argparse.SUPPRESS)
    p.add_argument("--decoder", default=None, help=argparse.SUPPRESS)
    p.add_argument("--ingest", default=None, help=argparse.SUPPRESS)
    p.add_argument("--latency", default=None, help=argparse.SUPPRESS)
    return p


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not os.path.isfile(args.hef):
        parser.error(f"HEF not found: {args.hef}")
    if args.publish_size is None:
        args.publish_size = args.work_size
    for name in ("decoder", "ingest", "latency"):
        if getattr(args, name) is not None:
            log.warning("--%s is no longer used and was ignored", name)
    return args


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=args.log_level, format="[%(name)s] %(message)s", stream=sys.stdout)
    work_w, work_h = args.work_size
    pub_w, pub_h = args.publish_size
    stats = Stats()

    stop = threading.Event()

    def on_signal(signum: int, _frame) -> None:
        log.info("received signal %d, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    detector: Optional[HailoDetector] = None
    source: Optional[FfmpegSource] = None
    publisher: Optional[RtspPublisher] = None
    try:
        detector = HailoDetector(args.hef, conf=args.conf, iou=args.iou, labels=args.labels, scores_mode=args.scores)
        if work_w < detector.in_w:
            log.warning("work size %dx%d is narrower than the model input %dx%d; frames will be upscaled",
                        work_w, work_h, detector.in_w, detector.in_h)

        source = FfmpegSource(args.source, work_w, work_h, out_fps=args.ingest_fps,
                              hwaccel=args.hwaccel, ffmpeg=args.ffmpeg, stats=stats)
        if not source.wait_ready(STARTUP_TIMEOUT_S):
            log.error("no frames from %s within %.0f s (ffmpeg exit code %s)",
                      args.source, STARTUP_TIMEOUT_S, source.returncode)
            return EXIT_STARTUP
        log.info("ingest: %s, %s fps -> %dx%d BGR", "hwaccel " + args.hwaccel if args.hwaccel else "software decode",
                 args.ingest_fps or "source", work_w, work_h)

        if not args.no_nice:
            # The decoder (already running at nice 0) must always win the CPU: if it falls
            # behind the camera, latency piles up upstream where nothing can drop frames.
            os.nice(MAIN_NICE)
        publisher = RtspPublisher(args.publish, pub_w, pub_h, fps=args.fps, bitrate=args.bitrate,
                                  bufsize=args.bufsize, nice_extra=0 if args.no_nice else ENCODER_NICE_EXTRA,
                                  ffmpeg=args.ffmpeg, stats=stats, codec=args.codec)
        log.info("publishing %s (%s %dx%d @ %d fps, %s)", args.publish, args.codec, pub_w, pub_h, args.fps, args.bitrate)

        return _loop(args, stats, stop, detector, source, publisher)
    finally:
        if publisher is not None:
            publisher.close()
        if source is not None:
            source.close()
        if detector is not None:
            detector.close()
        log.info("stopped")


def _loop(args, stats: Stats, stop: threading.Event, detector, source, publisher) -> int:
    """Process frames until stopped or a stage dies. Kept free of construction so
    it can be exercised with fakes. Uses only args.report, args.no_stamp,
    args.no_crosshair and args.publish."""
    stamp = not args.no_stamp
    crosshair = not args.no_crosshair
    timing_log = logging.getLogger("timing")
    seq = 0
    last_report = time.perf_counter()
    last_count = 0
    while not stop.is_set():
        if not publisher.alive():
            log.error("encoder exited with code %s; could not publish to %s", publisher.returncode, args.publish)
            return EXIT_PUBLISHER_DIED
        item = source.wait_new(seq, LOOP_WAIT_S)
        if item is None:
            if not source.alive():
                log.error("input stream ended")
                return EXIT_SOURCE_ENDED
        else:
            seq, t_arrival, frame = item
            t0 = time.perf_counter()
            rgb, geom = detector.preprocess(frame)
            t1 = time.perf_counter()
            outputs = detector.run(rgb)
            t2 = time.perf_counter()
            detections = detector.postprocess(outputs, geom)
            t3 = time.perf_counter()
            draw(frame, detections, stamp, crosshair)
            t4 = time.perf_counter()
            publisher.push(frame)

            stats.add("age", t0 - t_arrival)
            stats.add("letterbox", t1 - t0)
            stats.add("hailo", t2 - t1)
            stats.add("post", t3 - t2)
            stats.add("draw", t4 - t3)
            stats.count("processed")
            last_count = len(detections)

        if time.perf_counter() - last_report >= args.report:
            timing_log.info("last %.0f s, %d detections in last frame\n%s", args.report, last_count, stats.report())
            last_report = time.perf_counter()
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> None:
    sys.exit(run(parse_args(argv)))
