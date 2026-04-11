#!/usr/bin/env python3
"""
G1 Hardware Diagnostic Tool
===========================
Directly tests G1 Microphone (UDP Multicast) and Speaker (DDS PlayStream)
without using PulseAudio or the full pipeline.

Usage:
  python3 scripts/test_g1_hardware.py --speaker   # Test robot head speaker
  python3 scripts/test_g1_hardware.py --mic       # Test robot body mics (save to wav)
"""

import argparse
import array
import math
import os
import socket
import struct
import sys
import time
import wave

# Configuration (matching g1_audio_driver.py)
MULTICAST_GROUP = "239.168.123.161"
MULTICAST_PORT = 5555
LOCAL_IP = "192.168.123.164"  # Default PC2 IP
DDS_INTERFACE = os.environ.get("G1_DDS_INTERFACE", "eth0")
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit


def test_speaker():
    """Plays a 440Hz sine wave tone through G1 speaker for 2 seconds."""
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

    print(f"🔊 Initializing DDS on {DDS_INTERFACE}...")
    ChannelFactoryInitialize(0, DDS_INTERFACE)
    time.sleep(0.5)

    client = AudioClient()
    client.Init()
    
    print("📢 Playing tone (440Hz) through G1 head speaker...")
    
    duration = 2.0  # seconds
    freq = 440.0
    amplitude = 10000
    chunk_ms = 50
    samples_per_chunk = int(SAMPLE_RATE * chunk_ms / 1000)
    
    stream_id = str(int(time.time()))
    start_time = time.time()
    
    try:
        while time.time() - start_time < duration:
            chunk = array.array('h')
            for i in range(samples_per_chunk):
                t = (i + (time.time() - start_time) * SAMPLE_RATE) / SAMPLE_RATE
                sample = int(amplitude * math.sin(2 * math.pi * freq * t))
                chunk.append(sample)
            
            # Send to G1
            client.PlayStream("diag_tool", stream_id, chunk.tobytes())
            time.sleep(chunk_ms / 1000.0 * 0.9)
            
        print("✅ Speaker test complete")
    except KeyboardInterrupt:
        print("⏹️ Interrupted")
    finally:
        client.PlayStop("diag_tool")


def test_microphone():
    """Captures multicast audio and transcribes it in real-time using Faster-Whisper."""
    from faster_whisper import WhisperModel
    import numpy as np

    print(f"🎤 Testing G1 Microphone with Real-time ASR (UDP Multicast on {MULTICAST_GROUP}:{MULTICAST_PORT})...")
    
    # Load small Whisper model for local testing
    print("⏳ Loading Whisper model (tiny.en)...")
    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    
    # Create multicast socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", MULTICAST_PORT))
    
    mreq = struct.pack("4s4s", 
                       socket.inet_aton(MULTICAST_GROUP), 
                       socket.inet_aton(LOCAL_IP))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(5.0)
    
    print("👂 Listening... Speak into the robot. (Press Ctrl+C to stop)")
    print("-" * 50)
    
    # Accumulate buffer for transcription (e.g. 2 seconds at a time)
    buffer = bytearray()
    target_buffer_size = SAMPLE_RATE * SAMPLE_WIDTH * 2  # 2 seconds of audio
    
    try:
        while True:
            try:
                data, addr = sock.recvfrom(8192)
                buffer.extend(data)
                
                # Simple volume meter (visual feedback)
                samples = np.frombuffer(data, dtype=np.int16)
                rms = np.sqrt(np.mean(samples.astype(np.float32)**2))
                meter = "|" * int(min(rms / 100, 30))
                print(f"\rLevel: {meter:<30}", end="", flush=True)

                if len(buffer) >= target_buffer_size:
                    # Convert to float32 for Whisper
                    audio_data = np.frombuffer(buffer, dtype=np.int16).astype(np.float32) / 32768.0
                    
                    # Transcribe
                    segments, info = model.transcribe(audio_data, beam_size=1)
                    for segment in segments:
                        if segment.text.strip():
                            print(f"\n[TRANSCRIPT] {segment.text.strip()}")
                    
                    buffer = bytearray() # Clear buffer
            except socket.timeout:
                print("\n❌ ERROR: No audio data received! Check your network/multicast settings.")
                break

    except KeyboardInterrupt:
        print("\n⏹️ Interrupted")
    finally:
        sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="G1 Hardware Test")
    parser.add_argument("--speaker", action="store_true", help="Test G1 Speaker")
    parser.add_argument("--mic", action="store_true", help="Test G1 Microphone")
    args = parser.parse_args()

    if args.speaker:
        test_speaker()
    elif args.mic:
        test_microphone()
    else:
        parser.print_help()
