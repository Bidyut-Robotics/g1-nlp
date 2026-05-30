import cv2
import zmq
import pyrealsense2 as rs
import numpy as np
import time

def main():
    print("="*50)
    print("Starting G1 Camera ZMQ Streamer")
    print("="*50)
    
    # Setup ZMQ Publisher
    context = zmq.Context()
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
