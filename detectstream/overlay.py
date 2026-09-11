"""Draw detections, a centre crosshair and a wall-clock stamp onto a frame (in place)."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

import cv2
import numpy as np

from .detector import Detection

BOX_COLOR = (0, 255, 0)
FONT = cv2.FONT_HERSHEY_SIMPLEX
CROSSHAIR_ARM = 12        # pixels from the centre to each tip
CROSSHAIR_GAP = 3         # pixels left clear around the exact centre


def draw(frame: np.ndarray, detections: Sequence[Detection], stamp: bool = True,
         crosshair: bool = True) -> np.ndarray:
    """Boxes with labels, a small + at the frame centre, and the local time bottom-left.

    The stamp records when the frame left the Pi. Photographing the display next
    to a stopwatch the camera is pointed at splits the end-to-end latency into
    camera->Pi (stopwatch to stamp) and Pi->screen (stamp to photo time)."""
    for d in detections:
        cv2.rectangle(frame, (d.x1, d.y1), (d.x2, d.y2), BOX_COLOR, 2)
        cv2.putText(frame, f"{d.label} {d.score:.2f}", (d.x1, max(0, d.y1 - 6)), FONT, 0.6, BOX_COLOR, 2)
    if crosshair:
        _draw_crosshair(frame)
    if stamp:
        text = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        origin = (8, frame.shape[0] - 10)
        cv2.putText(frame, text, origin, FONT, 0.7, (0, 0, 0), 4)
        cv2.putText(frame, text, origin, FONT, 0.7, (255, 255, 255), 2)
    return frame


def _draw_crosshair(frame: np.ndarray) -> None:
    """A small + at the exact frame centre, white with a black outline so it
    reads on any background. Drawn last so it is never hidden by a box."""
    h, w = frame.shape[:2]
    cx, cy = w // 2, h // 2
    segments = [
        ((cx - CROSSHAIR_ARM, cy), (cx - CROSSHAIR_GAP, cy)),
        ((cx + CROSSHAIR_GAP, cy), (cx + CROSSHAIR_ARM, cy)),
        ((cx, cy - CROSSHAIR_ARM), (cx, cy - CROSSHAIR_GAP)),
        ((cx, cy + CROSSHAIR_GAP), (cx, cy + CROSSHAIR_ARM)),
    ]
    for color, thickness in (((0, 0, 0), 4), ((255, 255, 255), 2)):
        for a, b in segments:
            cv2.line(frame, a, b, color, thickness, cv2.LINE_AA)
