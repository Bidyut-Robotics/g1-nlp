import cv2
import zmq
import pyrealsense2 as rs
import numpy as np
import time
import threading
import sys

# Unitree SDK
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

def tts_worker(context):
    print("[INFO] Starting Unitree DDS Audio Client on Robot...")
    interface = sys.argv[1] if len(sys.argv) > 1 else "eth0"
    try:
        ChannelFactoryInitialize(0, interface)
        time.sleep(1.0)
        audio_client = AudioClient()
        audio_client.Init()
        audio_client.SetVolume(100)
        print("[INFO] Robot Audio Client Ready.")
    except Exception as e:
        print(f"[ERROR] Could not start Audio Client: {e}")
        return

    tts_socket = context.socket(zmq.PULL)
    tts_socket.bind("tcp://0.0.0.0:5556")
    print("[INFO] ZMQ Network PULL socket listening for TTS on port 5556.")

    while True:
        try:
            message = tts_socket.recv_string()
            print(f"[INFO] Received TTS command from Thor: '{message}'")
            audio_client.TtsMaker(message, 1)
        except Exception as e:
            print(f"[ERROR] TTS ZMQ Error: {e}")
            time.sleep(1)

def main():
    print("="*50)
    print("Starting G1 Camera ZMQ Streamer & TTS Listener")
    print("="*50)
    
    # Setup ZMQ Context
    context = zmq.Context()
    
    # Start TTS background listener thread
    threading.Thread(target=tts_worker, args=(context,), daemon=True).start()

    # Setup ZMQ Publisher for Camera frames
    socket = context.socket(zmq.PUB)
    # Bind to port 5555 on all network interfaces
    socket.bind("tcp://0.0.0.0:5555")
    
    print("[INFO] ZMQ Network Publisher bound to port 5555.")

    # Setup RealSense
    pipeline = rs.pipeline()
    config = rs.config()
    config.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 30)
    
    try:
        pipeline.start(config)
        print("[INFO] RealSense Camera started successfully.")
    except Exception as e:
        print(f"[ERROR] Could not start camera: {e}")
        print("Ensure no other script is using the camera and it is plugged in.")
        return

    print("[INFO] Streaming frames over network to AGX Thor...")
    
    try:
        while True:
            # Wait for a coherent pair of frames
            frames = pipeline.wait_for_frames()
            color_frame = frames.get_color_frame()
            if not color_frame:
                continue

            # Convert to numpy array
            color_image = np.asanyarray(color_frame.get_data())
            
            # Encode as JPEG to save network bandwidth
            ret, buffer = cv2.imencode('.jpg', color_image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ret:
                # Send the byte string over ZMQ
                socket.send(buffer.tobytes())
                
    except KeyboardInterrupt:
        print("\n[INFO] Stopping stream...")
    finally:
        pipeline.stop()
        socket.close()

if __name__ == "__main__":
    main()
