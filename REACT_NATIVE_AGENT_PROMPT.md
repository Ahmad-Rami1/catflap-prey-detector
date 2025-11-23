# React Native Agent Prompt: WebRTC Video Streaming Implementation

## Task Overview
Replace the current MJPEG WebView-based video streaming with WebRTC streaming for lower latency and better performance.

---

## Current Implementation

### File: The Control Screen Component (shown in user's code)
- Uses `react-native-webview` to display MJPEG stream
- Stream URL: `http://${baseUrl}:${videoPort}/cam` (currently port 8081)
- Video is rotated 180 degrees via CSS transform

### Current Dependencies
```json
{
  "react-native-webview": "^x.x.x",
  "expo-linear-gradient": "^x.x.x"
}
```

---

## Required Changes

### 1. Install WebRTC Package

Install the WebRTC package for React Native:
```bash
npm install react-native-webrtc
# or
yarn add react-native-webrtc
```

For Expo projects, you may need to create a development build:
```bash
npx expo install react-native-webrtc
npx expo prebuild
```

**Important**: `react-native-webrtc` is NOT compatible with Expo Go. You'll need to create a development build.

---

### 2. Update the Control Screen Component

Replace the WebView video streaming section with WebRTC implementation.

#### Key Changes Needed:

**Import WebRTC components:**
```typescript
import {
  RTCView,
  RTCPeerConnection,
  RTCSessionDescription,
  mediaDevices,
  MediaStream,
} from 'react-native-webrtc';
```

**State management:**
```typescript
const [remoteStream, setRemoteStream] = useState<MediaStream | null>(null);
const [peerConnection, setPeerConnection] = useState<RTCPeerConnection | null>(null);
```

**WebRTC Connection Function:**
```typescript
const startWebRTCStream = async () => {
  try {
    // Create RTCPeerConnection
    const configuration = {
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
    };

    const pc = new RTCPeerConnection(configuration);

    // Handle incoming remote stream
    pc.onaddstream = (event: any) => {
      console.log('Received remote stream');
      setRemoteStream(event.stream);
    };

    // Handle ICE connection state changes
    pc.oniceconnectionstatechange = () => {
      console.log('ICE connection state:', pc.iceConnectionState);
    };

    // Create offer
    const offer = await pc.createOffer({
      offerToReceiveVideo: true,
      offerToReceiveAudio: false,
    });

    await pc.setLocalDescription(offer);

    // Send offer to server
    const response = await fetch(`http://${baseUrl}:${videoPort}/offer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sdp: pc.localDescription.sdp,
        type: pc.localDescription.type,
      }),
    });

    const answer = await response.json();

    // Set remote description (answer from server)
    await pc.setRemoteDescription(
      new RTCSessionDescription({
        sdp: answer.sdp,
        type: answer.type,
      })
    );

    setPeerConnection(pc);
    console.log('WebRTC connection established');

  } catch (error) {
    console.error('Error starting WebRTC stream:', error);
    Alert.alert('WebRTC Error', `Failed to connect: ${error.message}`);
  }
};

const stopWebRTCStream = () => {
  if (peerConnection) {
    peerConnection.close();
    setPeerConnection(null);
  }
  setRemoteStream(null);
};
```

**Replace WebView with RTCView:**
```typescript
{/* OLD: WebView for MJPEG */}
<WebView
  key={videoKey}
  source={{ uri: getStreamUrl() }}
  style={styles.webview}
  ...
/>

{/* NEW: RTCView for WebRTC */}
{remoteStream ? (
  <RTCView
    streamURL={remoteStream.toURL()}
    style={styles.rtcView}
    objectFit="cover"
    mirror={false}
  />
) : (
  <View style={styles.loadingContainer}>
    <ActivityIndicator size="large" color={Colors.primary[500]} />
    <Text style={styles.loadingText}>
      {peerConnection ? 'Connecting...' : 'Tap refresh to start stream'}
    </Text>
  </View>
)}
```

**Update refresh button to start/stop WebRTC:**
```typescript
const refreshVideo = () => {
  if (peerConnection) {
    stopWebRTCStream();
  }
  startWebRTCStream();
};

// Auto-start stream when component mounts (optional)
useEffect(() => {
  if (baseUrl) {
    startWebRTCStream();
  }

  return () => {
    stopWebRTCStream();
  };
}, [baseUrl, videoPort]);
```

**Add RTCView style:**
```typescript
rtcView: {
  flex: 1,
  backgroundColor: '#000',
  transform: [{ rotate: '180deg' }], // Keep 180° rotation if needed
},
```

---

### 3. Configuration Updates

**Update video port default** (if needed):
- Current MJPEG: Port 8081 → `/cam` endpoint
- New WebRTC: Port 8081 → `/offer` endpoint

The `videoPort` state should remain `8081`.

**Server URL construction:**
```typescript
const getWebRTCOfferUrl = () => {
  if (!baseUrl) return '';
  return `http://${baseUrl}:${videoPort}/offer`;
};
```

---

### 4. Cleanup on Component Unmount

Ensure WebRTC connection is closed when leaving the screen:
```typescript
useFocusEffect(
  useCallback(() => {
    return () => {
      // Cleanup when tab loses focus
      stopWebRTCStream();
    };
  }, [peerConnection])
);
```

---

## Testing Checklist

1. **Install Dependencies**
   ```bash
   npm install react-native-webrtc
   npx expo prebuild  # For Expo projects
   npx expo run:ios   # or run:android
   ```

2. **Configure Pi 5 IP in App**
   - Go to Config tab
   - Set baseUrl to Pi 5 IP (e.g., `192.168.1.x`)
   - Set videoPort to `8081`

3. **Verify WebRTC Server is Running**
   ```bash
   # On Pi 5
   curl http://localhost:8081/health
   # Should return: {"status":"healthy","camera":"running","active_connections":0}
   ```

4. **Test in App**
   - Open Control screen
   - Video should auto-connect via WebRTC
   - Stream should be visible with low latency
   - Tap refresh button to reconnect

---

## Error Handling

Add proper error handling for common issues:

```typescript
const startWebRTCStream = async () => {
  if (!baseUrl) {
    Alert.alert('Configuration Required', 'Please set the Pi IP address in Config tab');
    return;
  }

  setIsLoading(true);

  try {
    // ... WebRTC connection code ...

  } catch (error: any) {
    console.error('WebRTC error:', error);

    let errorMessage = 'Failed to connect to camera. ';

    if (error.message?.includes('network') || error.code === 'NETWORK_ERROR') {
      errorMessage += 'Check your network connection and Pi IP address.';
    } else if (error.message?.includes('timeout')) {
      errorMessage += 'Connection timeout. Ensure the WebRTC server is running on Pi 5.';
    } else {
      errorMessage += error.message;
    }

    Alert.alert('WebRTC Connection Error', errorMessage);

  } finally {
    setIsLoading(false);
  }
};
```

---

## Platform-Specific Notes

### iOS
- Requires camera permissions in `Info.plist` (even for receiving stream):
  ```xml
  <key>NSCameraUsageDescription</key>
  <string>Camera access for video streaming</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>Microphone access for audio streaming</string>
  ```

### Android
- Add permissions to `AndroidManifest.xml`:
  ```xml
  <uses-permission android:name="android.permission.CAMERA" />
  <uses-permission android:name="android.permission.RECORD_AUDIO" />
  <uses-permission android:name="android.permission.INTERNET" />
  ```

---

## Fallback to MJPEG (Optional)

If you want to support both WebRTC and MJPEG as fallback:

```typescript
const [streamType, setStreamType] = useState<'webrtc' | 'mjpeg'>('webrtc');

// Toggle button
<TouchableOpacity onPress={() => setStreamType(streamType === 'webrtc' ? 'mjpeg' : 'webrtc')}>
  <Text>Switch to {streamType === 'webrtc' ? 'MJPEG' : 'WebRTC'}</Text>
</TouchableOpacity>

// Render appropriate component
{streamType === 'webrtc' ? (
  <RTCView ... />
) : (
  <WebView ... />
)}
```

---

## Expected Performance Improvement

- **MJPEG (Current)**: ~500-1000ms latency, higher bandwidth
- **WebRTC (New)**: ~100-300ms latency, adaptive bitrate, lower bandwidth

---

## Summary of Changes

1. Install `react-native-webrtc` package
2. Replace `WebView` component with `RTCView`
3. Implement WebRTC peer connection logic
4. Connect to Pi 5's `/offer` endpoint (port 8081)
5. Handle connection lifecycle (start, stop, cleanup)
6. Add error handling and loading states
7. Test on physical device (WebRTC won't work in simulator)

---

## Questions or Issues?

If you encounter issues:
1. Check Pi 5 WebRTC server is running: `curl http://<pi-ip>:8081/health`
2. Verify network connectivity between phone and Pi 5
3. Check React Native logs: `npx expo start` or `npx react-native log-android/log-ios`
4. Test WebRTC server in browser first: `http://<pi-ip>:8081/`
