"""
WebRTC streaming server for Picamera2 using aiortc and FastAPI.

This server provides WebRTC video streaming for low-latency camera access.
Run on port 8081 to serve WebRTC stream to React Native app.
"""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import cv2
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaBlackhole
from av import VideoFrame
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from picamera2 import Picamera2
from libcamera import Transform, controls

from catflap_prey_detector.detection.config import camera_config

logger = logging.getLogger(__name__)

# Global camera instance
picam2: Picamera2 | None = None
pcs = set()  # Set of active peer connections


class PiCameraTrack(VideoStreamTrack):
    """
    Video track that streams frames from Picamera2.
    """

    def __init__(self):
        super().__init__()
        self.camera = picam2
        if self.camera is None:
            raise RuntimeError("Camera not initialized")
        logger.info("PiCameraTrack initialized")

    async def recv(self):
        """
        Receive the next video frame.

        Returns frames at ~30 FPS from the Picamera2.
        """
        pts, time_base = await self.next_timestamp()

        # Capture frame from Picamera2
        frame_array = self.camera.capture_array("main")

        # Convert from RGB to BGR for VideoFrame
        frame_bgr = cv2.cvtColor(frame_array, cv2.COLOR_RGB2BGR)

        # Create VideoFrame from numpy array
        frame = VideoFrame.from_ndarray(frame_bgr, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base

        return frame


class Offer(BaseModel):
    """WebRTC offer from client"""
    sdp: str
    type: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Lifespan context manager for startup and shutdown events"""
    global picam2

    try:
        logger.info("Initializing Picamera2 for WebRTC streaming...")
        picam2 = Picamera2()

        # Get sensor mode
        modes = picam2.sensor_modes
        mode = modes[camera_config.mode] if camera_config.mode < len(modes) else modes[1]

        # Create video configuration
        config = picam2.create_video_configuration(
            sensor={'output_size': mode['size'], 'bit_depth': mode['bit_depth']},
            transform=Transform(vflip=camera_config.vflip, hflip=camera_config.hflip),
            main={'size': camera_config.resolution, 'format': 'RGB888'},
        )

        picam2.configure(config)
        picam2.start()
        picam2.set_controls({"AfMode": controls.AfModeEnum.Continuous})

        logger.info(f"Camera started - Resolution: {camera_config.resolution}")

    except Exception as e:
        logger.error(f"Failed to start camera: {e}")
        raise

    yield

    # Cleanup
    try:
        logger.info("Shutting down WebRTC server...")

        # Close all peer connections
        coros = [pc.close() for pc in pcs]
        await asyncio.gather(*coros)
        pcs.clear()

        # Stop camera
        if picam2:
            picam2.stop()
            logger.info("Camera stopped")

    except Exception as e:
        logger.error(f"Error during shutdown: {e}")


app = FastAPI(
    title="Picamera2 WebRTC Server",
    description="WebRTC streaming server for Picamera2 using aiortc",
    version="1.0.0",
    lifespan=lifespan
)


HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>WebRTC Camera Stream</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            text-align: center;
            background-color: #f0f0f0;
            margin: 0;
            padding: 20px;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            background-color: white;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        h1 {
            color: #333;
        }
        video {
            width: 100%;
            max-width: 640px;
            border: 2px solid #ddd;
            border-radius: 5px;
        }
        button {
            margin: 10px;
            padding: 10px 20px;
            font-size: 16px;
            cursor: pointer;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>WebRTC Camera Stream</h1>
        <button onclick="startStream()">Start Stream</button>
        <button onclick="stopStream()">Stop Stream</button>
        <br><br>
        <video id="video" autoplay playsinline></video>
    </div>

    <script>
        let pc = null;

        async function startStream() {
            const video = document.getElementById('video');

            // Create peer connection
            pc = new RTCPeerConnection({
                iceServers: [{urls: 'stun:stun.l.google.com:19302'}]
            });

            // Handle incoming tracks
            pc.addEventListener('track', (evt) => {
                if (evt.track.kind === 'video') {
                    video.srcObject = evt.streams[0];
                }
            });

            // Create offer
            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);

            // Send offer to server
            const response = await fetch('/offer', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    sdp: pc.localDescription.sdp,
                    type: pc.localDescription.type
                })
            });

            const answer = await response.json();
            await pc.setRemoteDescription(new RTCSessionDescription(answer));
        }

        async function stopStream() {
            if (pc) {
                pc.close();
                pc = null;
            }
            document.getElementById('video').srcObject = null;
        }
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main camera viewing page with WebRTC test"""
    return HTML_PAGE


@app.post("/offer")
async def offer(params: Offer):
    """
    Handle WebRTC offer from client and return answer.

    This is the signaling endpoint for WebRTC connection establishment.
    """
    try:
        logger.info("Received WebRTC offer")

        # Create RTCPeerConnection
        pc = RTCPeerConnection()
        pcs.add(pc)

        # Create video track from camera
        video_track = PiCameraTrack()
        pc.addTrack(video_track)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info(f"Connection state: {pc.connectionState}")
            if pc.connectionState == "failed":
                await pc.close()
                pcs.discard(pc)

        # Set remote description (offer from client)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=params.sdp, type=params.type))

        # Create answer
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        logger.info("WebRTC connection established")

        return JSONResponse({
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type
        })

    except Exception as e:
        logger.error(f"Error handling WebRTC offer: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "camera": "running" if picam2 and picam2.started else "stopped",
        "active_connections": len(pcs)
    }


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    uvicorn.run(
        "webrtc_server:app",
        host="0.0.0.0",
        port=8081,
        reload=False,
        log_level="info"
    )
