"""Per-stage timing accumulator shared by the pipeline threads."""

from __future__ import annotations

import threading
import time


class Stats:
    """Thread-safe sums/counts/maxima per named stage, reset on each report().

    Stages used by the pipeline (see README for how to read them):
        ingest, age, letterbox, hailo, post, draw, write
    Counters: in (decoded frames), processed, out (encoded frames), out_dup.
    """

    STAGE_ORDER = ("ingest", "age", "letterbox", "hailo", "post", "draw", "write")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._timings: dict[str, list[float]] = {}  # name -> [sum, count, max]
        self._counts: dict[str, int] = {}
        self._t0 = time.perf_counter()

    def add(self, stage: str, seconds: float) -> None:
        with self._lock:
            entry = self._timings.setdefault(stage, [0.0, 0, 0.0])
            entry[0] += seconds
            entry[1] += 1
            if seconds > entry[2]:
                entry[2] = seconds

    def count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + n

    def report(self) -> str:
        """Multi-line summary of the window since the last report; resets the window."""
        with self._lock:
            elapsed = max(time.perf_counter() - self._t0, 1e-9)
            names = [n for n in self.STAGE_ORDER if n in self._timings]
            names += sorted(n for n in self._timings if n not in self.STAGE_ORDER)
            lines = []
            for name in names:
                total, n, peak = self._timings[name]
                lines.append(f"  {name:<10} avg {1000 * total / n:6.1f} ms  max {1000 * peak:6.1f} ms  n={n}")
            if self._counts:
                lines.append("  " + "  ".join(f"{k} {v / elapsed:5.1f}/s" for k, v in sorted(self._counts.items())))
            self._timings.clear()
            self._counts.clear()
            self._t0 = time.perf_counter()
        return "\n".join(lines) if lines else "  (no samples)"
