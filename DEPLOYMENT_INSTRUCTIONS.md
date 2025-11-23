# Catflap Prey Detector - Deployment Instructions

## Changes Summary

### 1. Pi Zero 2 WH (100.78.10.14) - Cat Door API Extension ✅
- **File**: `~/catdoor-api/main.go`
- **New Endpoint**: `POST/GET /detected`
- **Functionality**:
  - Receives prey detection notifications from Pi 5
  - Sets cat door mode to RED (locked) via controller
  - Writes timestamp to `/home/rami/catdoor-config.json`
  - Auto-unlocks after 5 minutes (changes mode to GREEN)

### 2. Pi 5 - AI Prey Detector System ✅
- **Modified Files**:
  - `src/catflap_prey_detector/detection/config.py` - Added NotificationConfig
  - `src/catflap_prey_detector/hardware/catflap_controller.py` - Replaced GPIO lock with HTTP request
- **New Files**:
  - `src/catflap_prey_detector/detection/webrtc_server.py` - WebRTC streaming server
- **Behavior Changes**:
  - When prey detected → Sends HTTP GET to `http://100.78.10.14:8080/detected`
  - No longer uses GPIO/RFID jammer (Pi Zero handles mechanical locking)
  - Still sends Telegram notifications
  - Still saves images locally

### 3. React Native App - WebRTC Client
- **Required**: Implement WebRTC video streaming client
- **Details**: See REACT_NATIVE_AGENT_PROMPT.md

---

## Installation Steps

### On Pi Zero 2 WH (100.78.10.14)

```bash
# SSH into Pi Zero
ssh rami@100.78.10.14

# Navigate to catdoor-api directory
cd ~/catdoor-api

# Backup current main.go
cp main.go main.go.backup

# Replace main.go with new code
nano main.go
# Paste the new code provided, save with Ctrl+X, Y, Enter

# Rebuild
go build -o catdoor-api main.go

# Restart service (adjust command based on how you run it)
# Option 1: If using systemd
sudo systemctl restart catdoor-api

# Option 2: If running manually
pkill catdoor-api
./catdoor-api &

# Verify it's running
curl http://localhost:8080/status
```

**Test the /detected endpoint:**
```bash
# Should lock the door and return JSON response
curl http://100.78.10.14:8080/detected

# Check the config file was created
cat /home/rami/catdoor-config.json
```

---

### On Pi 5 (AI Prey Detector)

```bash
# Navigate to project directory
cd ~/codebases/catflap-prey-detector

# Install new dependencies (aiortc and av packages)
uv sync

# Test WebRTC server standalone
uv run python -m catflap_prey_detector.detection.webrtc_server

# Should output:
# INFO:     Started server process
# INFO:     Waiting for application startup.
# INFO:     Initializing Picamera2 for WebRTC streaming...
# INFO:     Camera started - Resolution: (640, 360)
# INFO:     Application startup complete.
# INFO:     Uvicorn running on http://0.0.0.0:8081
```

**Test WebRTC in browser:**
1. Open browser: `http://<pi5-ip>:8081/`
2. Click "Start Stream"
3. You should see live video from camera

**Test prey detection HTTP notification:**
```bash
# Verify config
uv run python -c "from catflap_prey_detector.detection.config import notification_config; print(notification_config.catdoor_api_url)"
# Should output: http://100.78.10.14:8080/detected

# Run main detector (will automatically use new HTTP notification)
uv run catflap-detector
```

---

## Running WebRTC Server as a Service

**Create systemd service for WebRTC:**
```bash
sudo nano /etc/systemd/system/catflap-webrtc.service
```

Paste:
```ini
[Unit]
Description=Catflap WebRTC Streaming Server
After=network.target

[Service]
Type=simple
User=rami
WorkingDirectory=/home/rami/codebases/catflap-prey-detector
ExecStart=/home/rami/.local/bin/uv run python -m catflap_prey_detector.detection.webrtc_server
Restart=always
RestartSec=5
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable catflap-webrtc
sudo systemctl start catflap-webrtc
sudo systemctl status catflap-webrtc

# View logs
journalctl -u catflap-webrtc -f
```

---

## Testing the Full Flow

1. **Trigger a prey detection** (manually or wait for real detection)
2. **Expected behavior**:
   - Pi 5 detects prey via YOLO + API
   - Pi 5 sends HTTP GET to `http://100.78.10.14:8080/detected`
   - Pi Zero receives request
   - Pi Zero sets mode to RED (locked)
   - Pi Zero writes timestamp to config file
   - Pi Zero auto-unlocks after 5 minutes
   - Pi 5 sends Telegram notification with image

3. **Verify on Pi Zero**:
```bash
# Check cat door API logs
# Should show: "🚨 Prey detected! Locking catflap..."

# Check config file
cat /home/rami/catdoor-config.json
# Should show last_detected timestamp and locked_until

# Wait 5 minutes, then check logs again
# Should show: "⏰ Auto-unlocking catflap after 5 minutes..."
```

4. **Verify on Pi 5**:
```bash
# Check logs
tail -f runtime/logs/main_app.log
# Should show: "Sending prey detection notification to http://100.78.10.14:8080/detected"
# Should show: "Cat door API response: {'status': 'locked', ...}"
```

---

## Environment Variables (Optional Customization)

You can override the default cat door API URL:

```bash
# On Pi 5, create .env file or export
export CATDOOR_API_URL="http://100.78.10.14:8080/detected"
```

---

## Troubleshooting

### Pi Zero not receiving requests
```bash
# Check if API is running
curl http://100.78.10.14:8080/status

# Check firewall
sudo ufw status
sudo ufw allow 8080/tcp

# Test from Pi 5
curl http://100.78.10.14:8080/detected
```

### Pi 5 can't reach Pi Zero
```bash
# Ping test
ping 100.78.10.14

# Port test
telnet 100.78.10.14 8080
```

### WebRTC not working
```bash
# Check if server is running
curl http://<pi5-ip>:8081/health

# Check camera
libcamera-still -o test.jpg

# Check dependencies
uv run python -c "import aiortc; import av; print('OK')"
```

---

## Rollback Instructions

### Pi Zero
```bash
cd ~/catdoor-api
cp main.go.backup main.go
go build -o catdoor-api main.go
sudo systemctl restart catdoor-api
```

### Pi 5
```bash
cd ~/codebases/catflap-prey-detector
git checkout src/catflap_prey_detector/detection/config.py
git checkout src/catflap_prey_detector/hardware/catflap_controller.py
rm src/catflap_prey_detector/detection/webrtc_server.py
git checkout pyproject.toml
```
