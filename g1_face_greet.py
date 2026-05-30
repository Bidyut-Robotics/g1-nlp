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
frame_lock = threading.Lock()
is_running = True
detected_faces = []

speech_queue = queue.Queue()

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

def detection_worker(recognition_data):
    global detected_faces, is_running
    
    log.info("Loading InsightFace ONNX Models...")
    face_app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
    face_app.prepare(ctx_id=0, det_size=(640, 640))
    
    last_greeted = {}
    
    while is_running:
        with frame_lock:
            frame_data = latest_frame_bytes
            
        if frame_data is None:
            time.sleep(0.01)
            continue
            
        # Decode JPEG from ZMQ
        frame_np = np.frombuffer(frame_data, dtype=np.uint8)
        frame = cv2.imdecode(frame_np, cv2.IMREAD_COLOR)
        if frame is None:
            continue
            
        small = cv2.resize(frame, (0, 0), fx=FRAME_SCALE, fy=FRAME_SCALE)
        faces = face_app.get(small)
        
        results = []
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

            results.append({
                "bbox": bbox,
                "name": name,
                "conf": conf
            })
            
        detected_faces = results
        time.sleep(0.01)

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

    # Start Workers
    threading.Thread(target=zmq_receiver_worker, daemon=True).start()
    threading.Thread(target=tts_worker, daemon=True).start()
    threading.Thread(target=detection_worker, args=(recognition_data,), daemon=True).start()

    log.info("G1 Face Greeting System Started. Press 'q' on the video window or Ctrl+C to quit.")

    try:
        while True:
            with frame_lock:
                frame_data = latest_frame_bytes
                
            if frame_data is not None:
                frame_np = np.frombuffer(frame_data, dtype=np.uint8)
                frame = cv2.imdecode(frame_np, cv2.IMREAD_COLOR)
                
                if frame is not None:
                    # Draw bounding boxes and labels
                    for res in detected_faces:
                        bbox = res["bbox"]
                        name = res["name"]
                        conf = res["conf"]
                        
                        color = (0, 255, 0) if name != "Unknown" else (0, 0, 255)
                        label = f"{name} ({conf}%)" if name != "Unknown" else "Unknown"
                        
                        cv2.rectangle(frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), color, 2)
                        cv2.putText(frame, label, (bbox[0], bbox[1] - 10),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                                    
                    cv2.imshow("G1 Face Recognition (AGX Thor)", frame)
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        break
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
