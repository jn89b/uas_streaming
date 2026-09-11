"""detectstream: Hailo object detection on a live RTSP camera stream, republished
as an annotated RTSP stream through MediaMTX.

    camera --> MediaMTX /eo --> FfmpegIngest --> HailoDetector --> draw --> RtspPublisher --> MediaMTX /detections

Governing rule: no stage ever queues frames. Every hand-off keeps only the newest
frame, so a slow consumer costs freshness, never latency.
"""

__version__ = "1.0.0"
