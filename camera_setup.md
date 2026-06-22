# APTI IP Camera → MediaMTX → Tailscale RTSP Streaming

This setup keeps the APTI camera reachable at `192.168.1.168`, pulls its RTSP video into MediaMTX on the Raspberry Pi, and lets other devices on Tailscale view the restream.

## Network Layout

```text
APTI Camera
IP: 192.168.1.168
RTSP source: rtsp://admin:CAMERA_PASSWORD@192.168.1.168:80/0

        Ethernet

Raspberry Pi
eth0 secondary IP: 192.168.1.10/24
MediaMTX RTSP server: port 8554
Tailscale: used for remote viewers
```

* Camera main stream: `/0`
* Camera substream: `/1`
* MediaMTX restream path: `/apti`
* Remote RTSP endpoint:

```text
rtsp://PI_TAILSCALE_IP:8554/apti
```

---

## 1. Keep the Camera Network IP on the Pi After Reboot

The Pi needs a static address on the camera subnet so it can always reach the camera at `192.168.1.168`.

Create the systemd service:

```bash
sudo tee /etc/systemd/system/camera-ethernet-ip.service > /dev/null <<'EOF'
[Unit]
Description=Add static IP for APTI camera network
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/ip address replace 192.168.1.10/24 dev eth0
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now camera-ethernet-ip.service
```

Verify the IP was added:

```bash
ip addr show eth0
ip route get 192.168.1.168
```

You should see:

```text
192.168.1.10/24
```

on `eth0`, and the route should use `eth0`.

---

## 2. Verify the Camera Is Reachable

Test the camera web interface:

```bash
curl -v --connect-timeout 5 http://192.168.1.168
```

Test the camera RTSP stream directly:

```bash
ffplay -rtsp_transport tcp \
  "rtsp://admin:CAMERA_PASSWORD@192.168.1.168:80/0"
```

For a lower-bandwidth stream, use:

```bash
ffplay -rtsp_transport tcp \
  "rtsp://admin:CAMERA_PASSWORD@192.168.1.168:80/1"
```

Replace `CAMERA_PASSWORD` with the camera’s actual password.

---

## 3. Create `docker-compose.yml`

Create or replace `docker-compose.yml`:

```yaml
services:
  mediamtx:
    image: bluenviron/mediamtx:1.19.1
    container_name: mediamtx
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./mediamtx.yml:/mediamtx.yml:ro
```

`network_mode: host` allows MediaMTX to listen directly on the Pi’s LAN and Tailscale interfaces.

---

## 4. Create `mediamtx.yml`

Create or replace `mediamtx.yml`:

```yaml
logLevel: info

rtsp: true
rtspAddress: :8554
rtspTransports: [tcp]

hls: false
webrtc: false
rtmp: false
srt: false

writeQueueSize: 1024

paths:
  apti:
    source: rtsp://admin:CAMERA_PASSWORD@192.168.1.168:80/0
    rtspTransport: tcp
    sourceOnDemand: false
```

Replace:

```text
CAMERA_PASSWORD
```

with the actual camera password.

For lower bandwidth, change `/0` to `/1`:

```yaml
source: rtsp://admin:CAMERA_PASSWORD@192.168.1.168:80/1
```

---

## 5. Start MediaMTX

From the folder containing both files:

```bash
docker compose up -d
docker compose logs -f mediamtx
```

A successful startup should include:

```text
[path apti] stream is available and online
[RTSP] started with listeners on :8554
```

This confirms MediaMTX is pulling the camera stream and restreaming it.

---

## 6. Test the MediaMTX Restream Locally on the Pi

```bash
ffplay -rtsp_transport tcp rtsp://127.0.0.1:8554/apti
```

You can also use VLC:

```text
Media → Open Network Stream
rtsp://127.0.0.1:8554/apti
```

---

## 7. View the Stream Remotely Through Tailscale

Get the Pi’s Tailscale IP:

```bash
tailscale ip -4
```

Example output:

```text
100.92.85.38
```

From another device on Tailscale, open this in VLC or ffplay:

```text
rtsp://100.92.85.38:8554/apti
```

Example with ffplay:

```bash
ffplay -rtsp_transport tcp rtsp://100.92.85.38:8554/apti
```

Example with VLC:

```text
Media → Open Network Stream
rtsp://100.92.85.38:8554/apti
```

Replace `100.92.85.38` with the Pi’s actual Tailscale IP.

---

## 8. Useful Status and Debug Commands

Check MediaMTX container status:

```bash
docker compose ps
```

View recent MediaMTX logs:

```bash
docker compose logs --tail=100 mediamtx
```

Follow live MediaMTX logs:

```bash
docker compose logs -f mediamtx
```

Restart MediaMTX after changing `mediamtx.yml`:

```bash
docker compose restart mediamtx
```

Check whether RTSP port 8554 is listening:

```bash
sudo ss -ltnp | grep :8554
```

Check the camera static-IP service:

```bash
sudo systemctl status camera-ethernet-ip.service --no-pager
```

---

## 9. Notes

* The camera stream is currently H.265 video with G.711 audio.
* VLC and ffplay should handle the stream normally.
* Some TAK, browser, or low-power clients may not decode H.265 well.
* If a client cannot display the stream, change the camera’s encoder to H.264 through the camera web interface.
* No MediaMTX viewer authentication is enabled in this setup.
* Remote viewers must be allowed to reach the Pi through Tailscale. For users in another tailnet, share the Pi/device through Tailscale and ensure TCP port `8554` is permitted.
