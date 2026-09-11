"""Fixed-rate H.264 publisher into MediaMTX.

Frames are handed to an ffmpeg child over a pipe and encoded with libx264
(software; the Pi 5 has no hardware encoder). The publisher thread runs at a
fixed frame rate and repeats the last frame when no new one has arrived, so
the viewer always sees a steady stream regardless of inference speed.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Optional

import cv2
import numpy as np

from .stats import Stats

log = logging.getLogger("publish")

# Encoder settings per codec. Both are software; the Pi 5 has no hardware encoder.
# h264 is the default: x265 costs several times the CPU for bandwidth we don't need,
# and browsers' WebRTC (MediaMTX's web player) generally cannot play H.265.
CODEC_ARGS = {
    "h264": ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"],
    "h265": ["-c:v", "libx265", "-preset", "ultrafast", "-tune", "zerolatency", "-x265-params", "log-level=error"],
}


class RtspPublisher:
    def __init__(
        self,
        url: str,
        width: int,
        height: int,
        fps: int = 30,
        bitrate: str = "2M",
        bufsize: str = "100k",
        nice_extra: int = 0,
        ffmpeg: str = "ffmpeg",
        stats: Optional[Stats] = None,
        codec: str = "h264",
    ) -> None:
        if codec not in CODEC_ARGS:
            raise ValueError(f"codec must be one of {tuple(CODEC_ARGS)}, got {codec!r}")
        self.size = (width, height)
        self.fps = fps
        self._stats = stats
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._seq = 0
        self._stop = threading.Event()

        prefix = ["nice", "-n", str(nice_extra)] if nice_extra > 0 else []
        cmd = [
            *prefix, ffmpeg, "-loglevel", "error", "-nostdin",
            "-fflags", "nobuffer", "-probesize", "32", "-analyzeduration", "0",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
            *CODEC_ARGS[codec],
            "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bufsize,
            "-g", str(fps), "-pix_fmt", "yuv420p",
            "-f", "rtsp", "-rtsp_transport", "tcp", url,
        ]
        log.debug("starting: %s", " ".join(cmd))
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        self._thread = threading.Thread(target=self._loop, name="publisher", daemon=True)
        self._thread.start()

    # -- lifecycle -------------------------------------------------------------
    def alive(self) -> bool:
        return self._proc.poll() is None

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode

    def close(self, grace: float = 2.0) -> None:
        self._stop.set()
        self._thread.join(timeout=grace)
        try:
            self._proc.stdin.close()  # EOF lets ffmpeg finish the RTSP session cleanly
        except OSError:
            pass
        try:
            self._proc.wait(grace)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()

    # -- producer side ----------------------------------------------------------
    def push(self, frame: np.ndarray) -> None:
        """Hand over the newest annotated frame. Resize/serialise happen on the
        publisher thread so the caller is not slowed down."""
        with self._lock:
            self._frame = frame
            self._seq += 1

    # -- publisher thread -------------------------------------------------------
    def _loop(self) -> None:
        period = 1.0 / self.fps
        last_seq = -1
        payload: Optional[bytes] = None
        while not self._stop.is_set() and self._proc.poll() is None:
            t0 = time.perf_counter()
            with self._lock:
                frame, seq = self._frame, self._seq
            if frame is not None:
                if seq != last_seq:
                    if (frame.shape[1], frame.shape[0]) != self.size:
                        frame = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
                    payload = frame.tobytes()
                elif self._stats is not None:
                    self._stats.count("out_dup")
                last_seq = seq
                t_write = time.perf_counter()
                try:
                    self._proc.stdin.write(payload)
                except (BrokenPipeError, OSError):
                    log.error("encoder pipe closed")
                    return
                if self._stats is not None:
                    self._stats.add("write", time.perf_counter() - t_write)
                    self._stats.count("out")
            self._stop.wait(max(0.0, period - (time.perf_counter() - t0)))
