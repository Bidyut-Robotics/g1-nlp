#!/usr/bin/env python3
import os
import sys
import time
import logging
import multiprocessing
import queue
import threading
import pickle
import numpy as np
from pathlib import Path
from flask import Flask, Response, render_template_string

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

NETWORK_INTERFACE = sys.argv[1] if len(sys.argv) > 1 else "eth0"
RECOGNITION_THRESHOLD = 0.50
FRAME_SCALE = 0.5
GREET_COOLDOWN = 60
G1_IP = "192.168.123.164"
ZMQ_URL = f"tcp://{G1_IP}:5555"

SCRIPT_DIR = Path(__file__).parent.resolve()
ENCODINGS_FILE = SCRIPT_DIR / "bidyutfr" / "encodings.pkl"

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
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', sans-serif; }
        body { background: radial-gradient(circle at 50% -20%, #1e293b, var(--bg-dark) 80%); color: #f8fafc; min-height: 100vh; display: flex; flex-direction: column; align-items: center; padding: 2rem 1rem; }
        .header { text-align: center; margin-bottom: 2rem; }
        .title { font-size: 2.5rem; font-weight: 800; background: linear-gradient(to right, #818cf8, #c084fc); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 0.75rem; }
        .status-badge { display: inline-flex; align-items: center; gap: 0.5rem; background: rgba(16, 185, 129, 0.1); color: #10b981; padding: 0.5rem 1rem; border-radius: 2rem; font-weight: 600; font-size: 0.875rem; border: 1px solid rgba(16, 185, 129, 0.2); }
        .dashboard { width: 100%; max-width: 900px; background: var(--glass-bg); backdrop-filter: blur(16px); border: 1px solid var(--glass-border); border-radius: 24px; padding: 1.5rem; }
        .video-container { width: 100%; border-radius: 16px; overflow: hidden; background: #000; aspect-ratio: 16/9; }
        .video-container img { width: 100%; height: 100%; object-fit: contain; display: block; }
    </style>
</head>
<body>
    <div class="header">
        <h1 class="title">G1 Face Recognition Stream</h1>
        <div class="status-badge">System Active</div>
    </div>
    <div class="dashboard">
        <div class="video-container">
            <img src="{{ url_for('video_feed') }}" alt="Live Camera Feed" />
        </div>
    </div>
</body>
</html>
"""

# ==============================================================================
# TTS PROCESS: Runs in a completely isolated OS process to prevent
# buffer overflow / TLS conflicts between ONNX Runtime and Unitree CycloneDDS.
# ==============================================================================
def tts_worker_process(interface, text_queue):
    import time
    import logging
    log = logging.getLogger("TTS_Process")
    
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
        
        log.info(f"Initializing Unitree DDS Audio Client on interface {interface}...")
        ChannelFactoryInitialize(0, interface)
        time.sleep(1.0)
        
        audio_client = AudioClient()
        audio_client.Init()
        audio_client.SetVolume(100)
        log.info("Unitree Audio Client Ready. Connected to G1 Speaker.")
        
        while True:
            try:
                text = text_queue.get(timeout=1.0)
                log.info(f"Speaking: '{text}' on G1 Speaker...")
                audio_client.TtsMaker(text, 1)
            except queue.Empty:
                continue
    except Exception as e:
        log.error(f"TTS Process Error: {e}")

# Global state for Main Process
latest_frame_bytes = None
latest_annotated_frame = None
frame_lock = threading.Lock()
flask_frame_lock = threading.Lock()
is_running = True

# --- Flask Server Logic ---
def generate_frames():
    global latest_annotated_frame, is_running
    import cv2
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

# --- ZMQ Receiver Worker ---
def zmq_receiver_worker():
    global latest_frame_bytes
    import zmq
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

def compute_cosine_distance(emb1, emb2):
    return 1 - np.dot(emb1, emb2) / (np.linalg.norm(emb1) * np.linalg.norm(emb2))

def load_encodings():
    if not ENCODINGS_FILE.exists():
        log.error(f"{ENCODINGS_FILE} not found. Please ensure bidyutfr/encodings.pkl exists.")
        raise FileNotFoundError(str(ENCODINGS_FILE))
    with open(ENCODINGS_FILE, "rb") as f:
        data = pickle.load(f)
    log.info(f"Loaded {len(data['embeddings'])} embeddings for {len(set(data['names']))} unique people.")
    return data

def main():
    global is_running, latest_annotated_frame
    
    tts_queue = multiprocessing.Queue()
    tts_proc = multiprocessing.Process(target=tts_worker_process, args=(NETWORK_INTERFACE, tts_queue), daemon=True)
    tts_proc.start()

    import cv2
    import zmq
    from insightface.app import FaceAnalysis

    try:
        recognition_data = load_encodings()
    except Exception as e:
        log.error(f"Failed to load encodings: {e}")
        return

    log.info("Loading InsightFace ONNX Models in main thread...")
    face_app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
    face_app.prepare(ctx_id=0, det_size=(640, 640))

    threading.Thread(target=zmq_receiver_worker, daemon=True).start()
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
                    small = cv2.resize(frame, (0, 0), fx=FRAME_SCALE, fy=FRAME_SCALE)
                    faces = face_app.get(small)
                    for face in faces:
                        bbox = (face.bbox / FRAME_SCALE).astype(int)
                        unknown_emb = face.normed_embedding
                        min_dist, best_match = 1.0, "Unknown"
                        for i, known_emb in enumerate(recognition_data["embeddings"]):
                            dist = compute_cosine_distance(unknown_emb, known_emb)
                            if dist < min_dist:
                                min_dist, best_match = dist, recognition_data["names"][i]
                        name, conf = "Unknown", 0
                        if min_dist <= RECOGNITION_THRESHOLD:
                            name, conf = best_match, max(0, int((1 - min_dist) * 100))
                            now = time.time()
                            if name not in last_greeted or (now - last_greeted[name]) > GREET_COOLDOWN:
                                tts_queue.put(f"Hello, {name}! Good to see you.")
                                last_greeted[name] = now
                        color = (0, 255, 0) if name != "Unknown" else (0, 0, 255)
                        label = f"{name} ({conf}%)" if name != "Unknown" else "Unknown"
                        cv2.rectangle(frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), color, 2)
                        cv2.putText(frame, label, (bbox[0], bbox[1] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                    with flask_frame_lock:
                        latest_annotated_frame = frame.copy()
            else:
                time.sleep(0.1)
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    finally:
        is_running = False
        if tts_proc.is_alive():
            tts_proc.terminate()
        log.info("System Shut Down.")

if __name__ == "__main__":
    multiprocessing.set_start_method('spawn', force=True)
    main()
