# detectstream

Live object detection on the Gremsy payload's video with a Hailo AI HAT, published
back into MediaMTX as a second RTSP stream that the PyQt operator app can view.

```
camera ──H.265──> MediaMTX /eo ──> ingest ──> detector ──> overlay ──> publisher ──> MediaMTX /detections ──> laptop
                                  (ffmpeg)     (Hailo)               (libx264)
```

## Layout

```
detect_stream.py                 entry point (what the systemd unit runs)
detectstream/
    ingest.py                    FfmpegSource: RTSP pull + software decode, newest frame only
    detector.py                  HailoDetector + pure-numpy decoding of YOLO outputs
    overlay.py                   boxes and wall-clock stamp
    publisher.py                 RtspPublisher: fixed-rate H.264 into MediaMTX
    stats.py                     per-stage timing
    cli.py                       arguments, wiring, logging, shutdown
tests/                           unit tests (no HAT needed)
useful_shell_scripts/install_detect_service.sh
```

## The one rule

Never queue frames. Every stage hands the next one only the newest frame and
drops the rest, so latency stays flat no matter how slow any stage is.

The exception that shaped the design: H.265 frames can't be dropped *before*
decoding (each depends on the previous one), so the decoder must always run at
least as fast as the camera sends. If it can't, frames back up in MediaMTX and the
TCP buffers and you get a constant multi-second delay plus "reader is too slow"
in the MediaMTX log. That is why decoding is done in software by ffmpeg (a Pi 5
manages 720p HEVC at well over 100 fps) rather than the Pi's hardware decoder
(measured at ~36 fps on this stream and prone to hanging), and why the decoder
gets CPU priority over everything else.

## Running

```bash
python3 detect_stream.py --hef ~/hailo26/model.hef --work-size 1280x720 --publish-size 960x540
python3 detect_stream.py --help
```

`--codec h264` (default) or `h265`: H.265 needs fewer bits, but software x265
costs several times the encoder CPU and browsers' WebRTC cannot play it, so use
it only if the link, not the Pi, is the limit.

`--work-size` is the resolution frames are decoded to and the model sees; make it
at least the HEF's input size. `--publish-size` is the resolution of the stream
sent out; smaller is cheaper to encode and to ship over Tailscale. Keep both 16:9
so click-to-track in the viewer maps correctly. `--labels boat` (or a file) names
the classes; unset, an 80-class model gets COCO names.

Both kinds of HEF are handled automatically: Model Zoo HEFs with on-chip NMS, and
HEFs compiled without it (raw head outputs, YOLOv8 DFL or YOLO26 direct boxes),
which are decoded in `detector.py`. The startup log shows what was found.

Only one process can hold the HAT. Stop the service before running by hand:
`sudo systemctl stop detect-stream`.

## As a service

```bash
sudo HEF=/home/cuav7/hailo26/model.hef useful_shell_scripts/install_detect_service.sh
journalctl -fu detect-stream
```

Environment variables override the defaults (`WORK_SIZE`, `PUBLISH_SIZE`, `FPS`,
`LABELS`, `EXTRA_ARGS`). Rerun the installer to change them. The service restarts
itself if the camera stream drops or MediaMTX restarts.

Older units that pass `--out-size` or `--decoder` keep working: `--out-size` is
an alias for `--work-size`; `--decoder`, `--ingest` and `--latency` are accepted
and ignored with a warning.

MediaMTX needs the path to exist; in `mediamtx.yml`:

```yaml
  detections:
    source: publisher
```

## Reading the timing log

Every 5 s:

```
ingest     avg   33.4 ms  max   41.0 ms  n=150     interval between decoded frames; should be 1/--ingest-fps
age        avg    1.2 ms                             how long the newest frame waited for the loop
letterbox  avg    1.9 ms                             resize/pad/RGB for the model
hailo      avg   49.8 ms                             HAT round trip (the model's speed)
post       avg    3.1 ms                             box decode, NMS, mapping back to the frame
draw       avg    0.8 ms                             boxes and clock overlay
write      avg    2.4 ms                             handing a frame to x264; large = encoder can't keep up
in 30.0/s  out 30.0/s  out_dup 10.0/s  processed 20.0/s
```

`ingest` drifting above its target means the decoder isn't keeping up (check
`top` and the camera's frame rate with `ffprobe`). `out_dup` counts published
frames that repeated the previous one because the model hadn't finished; that is
normal when the model is slower than `--fps`.

The published picture carries a small crosshair at the exact centre (the
camera's boresight; `--no-crosshair` removes it) and the Pi's clock bottom-left. Point the camera at a
stopwatch and photograph the laptop screen: stopwatch-to-stamp is camera plus
Pi, stamp-to-photo is encoder plus network plus viewer.

## Tests

```bash
python3 -m unittest discover -s tests -v      # or: python3 -m pytest tests/
```

They cover the letterbox geometry, raw-head decoding (direct and DFL boxes,
logit-space thresholding, NMS), on-chip NMS decoding, label resolution, argument
validation, the processing loop (with fakes), the frame slot, the timing
report, and the ffmpeg ingest against a generated clip. No HAT or camera
required; the ingest test skips itself if ffmpeg is missing.
