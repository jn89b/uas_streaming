"""Camera ingest.

One ffmpeg process does everything: pulls the RTSP stream, decodes it (software
by default, frame-threaded across all cores), reduces the frame rate *after*
decoding, scales, converts to BGR and writes raw frames to a pipe. A reader
thread turns those into numpy arrays and drops them into a newest-wins slot.

Why ffmpeg rather than a GStreamer pipeline: it has no RTP jitter buffer and no
timestamp-scheduled release, so nothing upstream of the decoder can hoard
frames. Why decode every frame even when we only want 30 fps: H.265 frames
reference the ones before them, so the decoder must consume the camera's full
rate; the fps filter thins the result afterwards.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Optional

import numpy as np

from .stats import Stats

log = logging.getLogger("ingest")

RTSP_LOW_LATENCY_ARGS = [
    "-rtsp_transport", "tcp",
    "-fflags", "nobuffer", "-flags", "low_delay",
    "-probesize", "32", "-analyzeduration", "0", "-max_delay", "0",
]

Frame = np.ndarray


class FrameSlot:
    """Single-slot, newest-wins hand-off between a producer and one consumer.

    The producer must hand over arrays it will not touch again; the consumer
    then owns the frame it receives (no copy is made).
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame: Optional[Frame] = None
        self._seq = 0
        self._t_put = 0.0
        self._closed = False

    def put(self, frame: Frame, t_put: float) -> None:
        with self._cond:
            self._frame = frame
            self._seq += 1
            self._t_put = t_put
            self._cond.notify_all()

    def wait_new(self, after_seq: int, timeout: float) -> Optional[tuple[int, float, Frame]]:
        """Block until a frame newer than after_seq exists, the slot is closed, or
        timeout elapses. Returns (seq, t_put, frame) or None."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._seq == after_seq and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            if self._seq == after_seq:
                return None
            return self._seq, self._t_put, self._frame

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        return self._closed


def widen_pipe(fileobj, size: int = 1 << 20) -> None:
    """Grow a pipe's kernel buffer so a multi-MB raw frame crosses it in a few
    reads instead of dozens. Linux only; silently a no-op elsewhere."""
    try:
        import fcntl  # noqa: PLC0415 (platform specific)

        fcntl.fcntl(fileobj.fileno(), getattr(fcntl, "F_SETPIPE_SZ", 1031), size)
    except (ImportError, OSError, AttributeError):
        pass


class FfmpegSource:
    """Decoded frames from an ffmpeg child process, newest-wins.

    Args:
        source:   RTSP URL (low-latency input flags are applied) or any other
                  ffmpeg-readable input (used as-is, e.g. a file in tests).
        width, height: working frame size handed to the consumer.
        out_fps:  frame rate after decode (0 = keep the source rate).
        hwaccel:  optional ffmpeg -hwaccel name. Untested on Pi 5; software
                  decode handles 720p60 HEVC there with headroom.
        ffmpeg:   ffmpeg executable.
        stats:    optional Stats for 'ingest' timings and the 'in' counter.
    """

    def __init__(
        self,
        source: str,
        width: int,
        height: int,
        out_fps: int = 30,
        hwaccel: Optional[str] = None,
        ffmpeg: str = "ffmpeg",
        stats: Optional[Stats] = None,
    ) -> None:
        self.width, self.height = width, height
        self.frame_bytes = width * height * 3
        self.slot = FrameSlot()
        self._stats = stats
        self._ready = threading.Event()

        filters = []
        if hwaccel:
            filters.append("hwdownload,format=nv12")
        if out_fps > 0:
            filters.append(f"fps={out_fps}")
        filters.append(f"scale={width}:{height}:flags=bilinear")

        cmd = [ffmpeg, "-loglevel", "error", "-nostdin"]
        if source.startswith("rtsp://"):
            cmd += RTSP_LOW_LATENCY_ARGS
        if hwaccel:
            cmd += ["-hwaccel", hwaccel]
        cmd += ["-i", source, "-an",
                "-vf", ",".join(filters), "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]

        log.debug("starting: %s", " ".join(cmd))
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
        widen_pipe(self._proc.stdout)
        self._thread = threading.Thread(target=self._reader, name="ingest-reader", daemon=True)
        self._thread.start()

    # -- lifecycle -------------------------------------------------------------
    def wait_ready(self, timeout: float) -> bool:
        """True once the first frame has arrived."""
        return self._ready.wait(timeout)

    def wait_new(self, after_seq: int, timeout: float) -> Optional[tuple[int, float, Frame]]:
        """Newest frame with seq > after_seq, or None on timeout / end of stream."""
        return self.slot.wait_new(after_seq, timeout)

    def alive(self) -> bool:
        return self._proc.poll() is None and not self.slot.closed

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode

    def close(self, grace: float = 2.0) -> None:
        self.slot.close()
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(grace)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._thread.join(timeout=grace)
        self._proc.stdout.close()

    # -- reader thread -----------------------------------------------------------
    def _reader(self) -> None:
        out = self._proc.stdout
        last_arrival: Optional[float] = None
        while True:
            buf = bytearray(self.frame_bytes)  # fresh buffer per frame: consumer owns it afterwards
            view = memoryview(buf)
            got = 0
            while got < self.frame_bytes:
                n = out.readinto(view[got:])
                if not n:
                    log.info("decoder pipeline ended (ffmpeg exit code %s)", self._proc.poll())
                    self.slot.close()
                    return
                got += n
            now = time.perf_counter()
            if self._stats is not None:
                if last_arrival is not None:
                    self._stats.add("ingest", now - last_arrival)
                self._stats.count("in")
            last_arrival = now
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(self.height, self.width, 3)
            self.slot.put(frame, now)
            self._ready.set()
