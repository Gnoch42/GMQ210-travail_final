# Park Attendance Analysis System

 This system uses a camera installed in a park to detect people, track their movements, and convert their positions into GPS coordinates. Data is stored in a spatial database (PostGIS) for analysis in QGIS.

## Required Hardware

- Raspberry Pi 4 (4 GB RAM) with Raspberry Pi OS Lite
- IMX219 camera module (connected to the Pi's CSI port)
- 32 GB+ microSD card
- USB-C power supply for the Pi
- Mac (for processing and analysis)
- Both devices must be on the same WiFi network

## Architecture

```
Park                                      Office
┌─────────────────────┐                   ┌──────────────────────────────┐
│  Raspberry Pi 4     │                   │  Mac                         │
│                     │     WiFi          │                              │
│  IMX219 → MediaMTX ─┼──── RTSP ───────→│  OpenCV → YOLOv8 → Tracker  │
│           (stream)  │                   │           → Homography       │
│  Flask API          │                   │           → PostGIS          │
│  (camera control)   │                   │                              │
└─────────────────────┘                   │  QGIS (visualization)        │
                                          └──────────────────────────────┘
```

The camera captures images on the Pi, MediaMTX streams them over RTSP via WiFi, and all processing (detection, tracking, GPS conversion, storage) happens on the Mac.

---

## 1. Raspberry Pi Setup

### 1.1 Install Raspberry Pi OS Lite

Use Raspberry Pi Imager on the Mac to flash Raspberry Pi OS Lite (64-bit) onto the SD card. In the advanced settings, configure WiFi and enable SSH.

### 1.2 Connect to the Pi

```bash
ssh pi@<PI_IP_ADDRESS>
```

To find the Pi's IP address, check your router's interface or use `ping camerapi1.local`.

### 1.3 Install camera tools

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y rpicam-apps-lite
```

Verify the camera is detected:

```bash
rpicam-hello --list-cameras -v
```

### 1.4 Install MediaMTX (RTSP streaming server)

```bash
cd ~
wget https://github.com/bluenviron/mediamtx/releases/download/v1.11.3/mediamtx_v1.11.3_linux_arm64v8.tar.gz
tar -xzf mediamtx_v1.11.3_linux_arm64v8.tar.gz
sudo cp mediamtx /usr/local/bin/
sudo cp mediamtx.yml /usr/local/etc/mediamtx.yml
```

Edit the configuration to enable the camera:

```bash
sudo nano /usr/local/etc/mediamtx.yml
```

Scroll to the bottom of the file and replace the `paths:` section with:

```yaml
paths:
  parc:
    source: rpiCamera
  all_others:
```

Note the indentation: 2 spaces before `parc:`, 4 spaces before `source:`.

Give the pi user write permissions:

```bash
sudo chown pi:pi /usr/local/etc/mediamtx.yml
```

### 1.5 Create the systemd service for MediaMTX

```bash
sudo nano /etc/systemd/system/mediamtx.service
```

Content:

```
[Unit]
Description=MediaMTX RTSP Server
After=network-online.target

[Service]
ExecStart=/usr/local/bin/mediamtx /usr/local/etc/mediamtx.yml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
```

Verify it is running:

```bash
sudo systemctl status mediamtx
```

### 1.6 Test the RTSP stream

From the Mac, open VLC → File → Open Network Stream:

```
rtsp://<PI_IP_ADDRESS>:8554/parc
```

Or with ffplay (install ffmpeg via `brew install ffmpeg`):

```bash
ffplay rtsp://<PI_IP_ADDRESS>:8554/parc
```

### 1.7 Install the Flask control API

On the Pi, create the project:

```bash
mkdir -p ~/parc-camera
cd ~/parc-camera
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install flask psutil requests
```

Create the API file:

```bash
nano ~/parc-camera/camera_api.py
```

Content:

```python
from flask import Flask, jsonify, request
import subprocess

app = Flask(__name__)

CONFIG_PATH = "/usr/local/etc/mediamtx.yml"

@app.route("/health", methods=["GET"])
def health():
    result = subprocess.run(
        ["systemctl", "is-active", "mediamtx"],
        capture_output=True, text=True
    )
    is_running = result.stdout.strip() == "active"
    return jsonify({
        "mediamtx_running": is_running,
        "status": "healthy" if is_running else "down"
    })

@app.route("/stream/<action>", methods=["POST"])
def stream_control(action):
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "Invalid action"}), 400
    result = subprocess.run(
        ["sudo", "systemctl", action, "mediamtx"],
        capture_output=True, text=True
    )
    success = result.returncode == 0
    return jsonify({
        "message": f"Stream {action}" if success else f"Failed {action}",
        "success": success
    })

@app.route("/config", methods=["PUT"])
def update_config():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    mapping = {
        "width": "rpiCameraWidth",
        "height": "rpiCameraHeight",
        "fps": "rpiCameraFPS",
        "bitrate": "rpiCameraBitrate",
    }

    with open(CONFIG_PATH, "r") as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        stripped = line.strip()
        for param, yml_key in mapping.items():
            if param in data and stripped.startswith(yml_key + ":"):
                indent = line[:len(line) - len(line.lstrip())]
                lines[i] = f"{indent}{yml_key}: {data[param]}\n"

    with open(CONFIG_PATH, "w") as f:
        f.writelines(lines)

    result = subprocess.run(
        ["sudo", "systemctl", "restart", "mediamtx"],
        capture_output=True, text=True
    )
    success = result.returncode == 0
    return jsonify({
        "message": "Configuration applied" if success else "Restart failed",
        "config": data,
        "success": success
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
```

### 1.8 Configure passwordless sudo for systemctl

```bash
sudo visudo -f /etc/sudoers.d/mediamtx
```

Add this line:

```
pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl start mediamtx, /usr/bin/systemctl stop mediamtx, /usr/bin/systemctl restart mediamtx
```

### 1.9 Create the systemd service for the Flask API

```bash
sudo nano /etc/systemd/system/camera-api.service
```

Content:

```
[Unit]
Description=Camera Control Flask API
After=network-online.target mediamtx.service

[Service]
User=pi
WorkingDirectory=/home/pi/parc-camera
ExecStart=/home/pi/parc-camera/venv/bin/python camera_api.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now camera-api
```

### 1.10 Verify everything works on the Pi

```bash
# Check services
sudo systemctl status mediamtx
sudo systemctl status camera-api

# Test the API from the Mac
curl http://<PI_IP_ADDRESS>:5000/health
```

---

## 2. Mac Setup

### 2.1 Install prerequisites

```bash
# PostgreSQL and PostGIS
brew install postgresql@17 postgis
brew services start postgresql@17

# Add to PATH (if not already done)
echo 'export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# ffmpeg (for recording videos)
brew install ffmpeg
```

### 2.2 Create the database

```bash
createdb parc_frequentation
psql -d parc_frequentation -c "CREATE EXTENSION IF NOT EXISTS postgis;"
```

Create the tables:

```bash
psql -d parc_frequentation -c "
CREATE TABLE detections (
    id SERIAL PRIMARY KEY,
    camera_id VARCHAR(50) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    person_track_id INTEGER,
    pixel_x REAL,
    pixel_y REAL,
    location GEOMETRY(Point, 4326),
    location_32198 GEOMETRY(Point, 32198),
    confidence REAL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_detections_timestamp ON detections(timestamp);
CREATE INDEX idx_detections_location ON detections USING GIST(location);
CREATE INDEX idx_detections_location_32198 ON detections USING GIST(location_32198);

CREATE TABLE occupancy_snapshots (
    id SERIAL PRIMARY KEY,
    camera_id VARCHAR(50) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    person_count INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_snapshots_timestamp ON occupancy_snapshots(timestamp);
"
```

### 2.3 Install Python dependencies

```bash
cd ~/parc-analyse
python3 -m venv venv
source venv/bin/activate
pip install opencv-python ultralytics psycopg2-binary requests numpy
```

### 2.4 Place the project files

The project folder must contain:

```
project/
├── main.py              # Main pipeline
├── calibrate.py         # Calibration script
└── calibration.json     # Generated by calibrate.py
```

---

## 3. Calibration (homography)

Calibration maps pixel positions (in the camera image) to real GPS coordinates. It only needs to be done once, when the camera is installed in its final position.

### 3.1 Record a video from the field

```bash
ffmpeg -i rtsp://<PI_IP_ADDRESS>:8554/parc -c copy -t 60 ~/Desktop/capture_parc.mp4
```

### 3.2 Identify reference points

In the field, identify at least 4 fixed, clearly visible points within the camera's field of view: bench corners, path intersections, lamp post bases, etc. Record their GPS coordinates using Google Maps (long press → copy coordinates) or Google Earth Pro.

### 3.3 Run calibration

```bash
python calibrate.py
```

Update the video path in `calibrate.py` if needed (variable `VIDEO_PATH`).

The script displays a frame from the video. For each reference point:

1. Click on the point in the image
2. In the terminal, enter the GPS coordinates in the format: `45.379385, -71.929164`
3. Repeat for at least 4 points (more is better)
4. Type `q` in the terminal to finish

The script generates a `calibration.json` file containing the transformation matrix.

Important: the resolution of the calibration video must match the one used in `main.py`. If the RTSP stream is 1920x1080, calibrate with a 1920x1080 video.

---

## 4. Usage

### 4.1 Start the detection pipeline

```bash
python main.py
```

By default, `main.py` connects to the camera's RTSP stream. To analyze a recorded video, change the `STREAM_URL` variable in `main.py`:

```python
# Live stream
STREAM_URL = "rtsp://camerapi1.local:8554/parc"

# Recorded video
STREAM_URL = "/path/to/video.mp4"
```

### 4.2 Keyboard shortcuts (in the video window)

| Key | Action |
|-----|--------|
| `q` | Quit |
| `h` | Check camera health |
| `s` | Stop stream |
| `r` | Restart stream |
| `1` | High quality preset (1920x1080, 15fps) |
| `2` | Standard preset (1280x720, 15fps) |
| `3` | Economy preset (640x480, 10fps) |

Camera control shortcuts (h, s, r, 1, 2, 3) only work with the live stream, not with a recorded video.

### 4.3 Control the camera from the terminal

```bash
# Check status
curl http://<PI_IP_ADDRESS>:5000/health

# Stop / start / restart the stream
curl -X POST http://<PI_IP_ADDRESS>:5000/stream/stop
curl -X POST http://<PI_IP_ADDRESS>:5000/stream/start
curl -X POST http://<PI_IP_ADDRESS>:5000/stream/restart

# Change resolution
curl -X PUT http://<PI_IP_ADDRESS>:5000/config \
  -H "Content-Type: application/json" \
  -d '{"width": 1920, "height": 1080, "fps": 15, "bitrate": 4000000}'
```

### 4.4 Record the video stream

```bash
# Record for 1 hour
ffmpeg -i rtsp://<PI_IP_ADDRESS>:8554/parc -c copy -t 3600 ~/Desktop/capture.mp4

# Ctrl+C to stop early
```

---

## 5. Visualization in QGIS

### 5.1 Connect to PostGIS

In QGIS: Layer → Add Layer → PostGIS

Connection parameters:

| Field | Value |
|-------|-------|
| Host | localhost |
| Port | 5432 |
| Database | parc_frequentation |
| User | *(your Mac username)* |
| Password | *(leave blank)* |

### 5.2 Add layers

Select the `detections` table with the `location_32198` geometry column (in EPSG:32198, NAD83 / Quebec Lambert) so that points align with other Quebec layers.

### 5.3 Useful queries

```sql
-- Total number of detections
SELECT COUNT(*) FROM detections;

-- Latest occupancy snapshots
SELECT * FROM occupancy_snapshots ORDER BY timestamp DESC LIMIT 10;

-- Detections with GPS coordinates
SELECT pixel_x, pixel_y, ST_AsText(location), confidence
FROM detections WHERE location IS NOT NULL
ORDER BY timestamp DESC LIMIT 10;

-- Clear the database to start fresh
DELETE FROM detections;
DELETE FROM occupancy_snapshots;
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Camera not detected on Pi | Check the ribbon cable, try `rpicam-hello --list-cameras -v` |
| RTSP stream not working | `sudo systemctl status mediamtx` to see errors |
| Flask API not responding | `sudo systemctl status camera-api` to see errors |
| YOLOv8 detecting poorly | Adjust the confidence threshold (0.3 by default), check lighting |
| Multiple detections of the same person | Increase `max_distance` in the Tracker |
| GPS points offset in QGIS | Verify the layer uses `location_32198`, check the calibration |
| PostgreSQL won't start | `brew services restart postgresql@17` |
| "No space left" error on Pi | `TMPDIR=/home/pi/tmp pip install --no-cache-dir <package>` |
| Image cropped in calibrate.py | Adjust `DISPLAY_WIDTH` or use `cv2.WINDOW_NORMAL` |
