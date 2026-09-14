#!/usr/bin/env python3
"""Entry point kept at the repo root so the systemd unit's ExecStart stays stable.

    python3 detect_stream.py --hef hailo-models/26s_boat_coco_close.hef --work-size 1280x720 --publish-size 960x540
"""
from detectstream.cli import main

if __name__ == "__main__":
    main()
