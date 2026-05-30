#!/usr/bin/env python3
import os
import sys
import cv2
import pickle
import time
import logging
import numpy as np
import threading
import queue
import zmq
from pathlib import Path
from insightface.app import FaceAnalysis
from flask import Flask, Response, render_template_string

app = Flask(__name__)

# --- HTML/CSS Frontend Template ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>G1 Robot | Face Recognition Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #0f172a;
            --glass-bg: rgba(30, 41, 59, 0.7);
            --glass-border: rgba(255, 255, 255, 0.1);
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Inter', sans-serif;
        }

        body {
            background: radial-gradient(circle at 50% -20%, #1e293b, var(--bg-dark) 80%);
            color: #f8fafc;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 2rem 1rem;
        }

        .header {
            text-align: center;
            margin-bottom: 2rem;
            animation: fadeIn 1s ease-out;
        }

        .title {
            font-size: 2.5rem;
            font-weight: 800;
            background: linear-gradient(to right, #818cf8, #c084fc);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.75rem;
        }

        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            background: rgba(16, 185, 129, 0.1);
            color: #10b981;
            padding: 0.5rem 1rem;
            border-radius: 2rem;
            font-weight: 600;
            font-size: 0.875rem;
            border: 1px solid rgba(16, 185, 129, 0.2);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .pulse {
            width: 8px;
            height: 8px;
            background-color: #10b981;
            border-radius: 50%;
            box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
            animation: pulse-animation 2s infinite;
        }

        @keyframes pulse-animation {
            0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { box-shadow: 0 0 0 10px rgba(16, 185, 129, 0); }
            100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(-10px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .dashboard {
            width: 100%;
            max-width: 900px;
            background: var(--glass-bg);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid var(--glass-border);
            border-radius: 24px;
            padding: 1.5rem;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            animation: fadeIn 1s ease-out 0.2s backwards;
        }

        .video-container {
            width: 100%;
            border-radius: 16px;
            overflow: hidden;
            position: relative;
            background: #000;
            aspect-ratio: 16/9;
            box-shadow: inset 0 0 20px rgba(0,0,0,0.5);
        }

        .video-container img {
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: block;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1 class="title">G1 Face Recognition Stream</h1>
        <div class="status-badge">
            <div class="pulse"></div>
            System Active
        </div>
    </div>

    <div class="dashboard">
        <div class="video-container">
            <img src="{{ url_for('video_feed') }}" alt="Live Camera Feed" />
        </div>
    </div>
</body>
</html>
"""

# Unitree SDK2 Audio Client for DDS TTS
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

# Configurations
G1_IP = "192.168.123.164"
ZMQ_URL = f"tcp://{G1_IP}:5555"

# Setup Paths dynamically so it works on any system
SCRIPT_DIR = Path(__file__).parent.resolve()
ENCODINGS_FILE = SCRIPT_DIR / "bidyutfr" / "encodings.pkl"

NETWORK_INTERFACE = sys.argv[1] if len(sys.argv) > 1 else "eth0"

RECOGNITION_THRESHOLD = 0.50
FRAME_SCALE = 0.5
GREET_COOLDOWN = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Global state
latest_frame_bytes = None
latest_annotated_frame = None
frame_lock = threading.Lock()
flask_frame_lock = threading.Lock()
is_running = True

speech_queue = queue.Queue()

# --- Flask Server Logic ---
def generate_frames():
    global latest_annotated_frame, is_running
    while is_running:
        with flask_frame_lock:
            frame = latest_annotated_frame
            
        if frame is None:
            time.sleep(0.1)
            continue
            
        ret, buffer = cv2.imencode('.jpg', frame)
        if not ret:
            continue
            
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        time.sleep(1.0 / 30.0)

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

# --- Unitree Audio TTS Worker ---
def tts_worker():
    log.info(f"Initializing Unitree DDS Audio Client on interface {NETWORK_INTERFACE}...")
    try:
        ChannelFactoryInitialize(0, NETWORK_INTERFACE)
        time.sleep(1.0)
        audio_client = AudioClient()
        audio_client.Init()
        audio_client.SetVolume(100)
        log.info("Unitree Audio Client Ready. Connected to G1 Speaker.")

        while is_running:
            try:
                text = speech_queue.get(timeout=1)
                log.info(f"Speaking: '{text}' on G1 Speaker...")
                # 1 represents the voice type in TtsMaker
                ret = audio_client.TtsMaker(text, 1)
                if ret != 0:
                    log.error(f"TtsMaker returned error code {ret}")
                speech_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                log.error(f"TTS Worker Error: {e}")
    except Exception as e:
        log.error(f"Failed to initialize Unitree Audio Client: {e}")

# --- ZMQ Receiver Worker ---
def zmq_receiver_worker():
    global latest_frame_bytes
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.connect(ZMQ_URL)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")
    log.info(f"Connected to G1 ZMQ Camera Stream at {ZMQ_URL}")

    while is_running:
        try:
            frame_bytes = socket.recv(flags=zmq.NOBLOCK)
            with frame_lock:
                latest_frame_bytes = frame_bytes
        except zmq.Again:
            time.sleep(0.01)
        except Exception as e:
            log.error(f"ZMQ Error: {e}")
            time.sleep(1)

# --- Face Detection Logic ---
def compute_cosine_distance(emb1, emb2):
    return 1 - np.dot(emb1, emb2) / (np.linalg.norm(emb1) * np.linalg.norm(emb2))

# Detection worker removed, logic moved to main loop.

def load_encodings():
    if not ENCODINGS_FILE.exists():
        log.error(f"{ENCODINGS_FILE} not found. Please ensure bidyutfr/encodings.pkl exists.")
        raise FileNotFoundError(str(ENCODINGS_FILE))
    with open(ENCODINGS_FILE, "rb") as f:
        data = pickle.load(f)
    log.info("Loaded %d embeddings for %d unique people.",
             len(data["embeddings"]), len(set(data["names"])))
    return data

def main():
    global is_running
    
    try:
        recognition_data = load_encodings()
    except Exception as e:
        log.error(f"Failed to load encodings: {e}")
        return

    # Initialize InsightFace in the main thread to prevent Jetson buffer overflows
    log.info("Loading InsightFace ONNX Models in main thread...")
    face_app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
    face_app.prepare(ctx_id=0, det_size=(640, 640))

    # Start Background Workers
    threading.Thread(target=zmq_receiver_worker, daemon=True).start()
    threading.Thread(target=tts_worker, daemon=True).start()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=5000, use_reloader=False), daemon=True).start()

    log.info("G1 Face Greeting System Started.")
    log.info("=> OPEN YOUR BROWSER AND GO TO http://192.168.123.166:5000 TO VIEW THE CAMERA STREAM!")
    log.info("Press Ctrl+C to quit.")

    last_greeted = {}

    try:
        while True:
            with frame_lock:
                frame_data = latest_frame_bytes
                
            if frame_data is not None:
                frame_np = np.frombuffer(frame_data, dtype=np.uint8)
                frame = cv2.imdecode(frame_np, cv2.IMREAD_COLOR)
                
                if frame is not None:
                    # Run Face Detection in Main Thread
                    small = cv2.resize(frame, (0, 0), fx=FRAME_SCALE, fy=FRAME_SCALE)
                    faces = face_app.get(small)
                    
                    for face in faces:
                        bbox = (face.bbox / FRAME_SCALE).astype(int)
                        unknown_emb = face.normed_embedding
                        
                        min_dist = 1.0
                        best_match = "Unknown"
                        
                        for i, known_emb in enumerate(recognition_data["embeddings"]):
                            dist = compute_cosine_distance(unknown_emb, known_emb)
                            if dist < min_dist:
                                min_dist = dist
                                best_match = recognition_data["names"][i]
                        
                        name = "Unknown"
                        conf = 0
                        if min_dist <= RECOGNITION_THRESHOLD:
                            name = best_match
                            conf = max(0, int((1 - min_dist) * 100))
                            
                            now = time.time()
                            # Check cooldown to prevent spamming TTS
                            if name not in last_greeted or (now - last_greeted[name]) > GREET_COOLDOWN:
                                speech_queue.put(f"Hello, {name}! Good to see you.")
                                last_greeted[name] = now
                                log.info("RECOGNISED: %s", name)

                        color = (0, 255, 0) if name != "Unknown" else (0, 0, 255)
                        label = f"{name} ({conf}%)" if name != "Unknown" else "Unknown"
                        
                        cv2.rectangle(frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), color, 2)
                        cv2.putText(frame, label, (bbox[0], bbox[1] - 10),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                                    
                    # Update web dashboard frame
                    with flask_frame_lock:
                        latest_annotated_frame = frame.copy()
            else:
                time.sleep(0.1)
                
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    finally:
        is_running = False
        cv2.destroyAllWindows()
        log.info("System Shut Down.")

if __name__ == "__main__":
    main()
