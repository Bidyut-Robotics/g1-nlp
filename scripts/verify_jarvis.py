import os
import sys
import time
import numpy as np
import sounddevice as sd
from openwakeword.model import Model

# --- CONFIGURATION ---
# Dynamically find the project root so it works from any folder
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "jarvis.onnx")
THRESHOLD = 0.1  # Extremely low for debugging "dead" models
SAMPLE_RATE = 16000
CHUNK_SIZE = 1280 # Standard openwakeword chunk size

def test_wakeword():
    if not os.path.exists(MODEL_PATH):
        print(f"ERROR: Model file not found at {MODEL_PATH}")
        print("Please ensure you have uploaded jarvis.onnx to the models/ folder.")
        return

    print(f"--- [WAKEWORD TEST] ---")
    print(f"Loading model: {MODEL_PATH}")
    
    try:
        # We explicitly use onnx to avoid the tflite conflict
        oww_model = Model(wakeword_models=[MODEL_PATH], inference_framework="onnx")
        
        print(f"DEBUG: All loaded models: {list(oww_model.models.keys())}")
        
        if not oww_model.models:
            print("ERROR: No models were loaded! Check if the file is a valid openwakeword ONNX model.")
            return
            
        model_key = list(oww_model.models.keys())[0]
    except Exception as e:
        print(f"ERROR DURING INITIALIZATION: {e}")
        return
    print(f"Model loaded successfully. Key: '{model_key}'")
    print(f"Listening... (Press Ctrl+C to stop)")
    print(f"PROMPT: Say 'Jarvis' clearly into your microphone.")
    print("-" * 40)

    # Queue for audio chunks
    import queue
    audio_q = queue.Queue()

    def audio_callback(indata, frames, time, status):
        if status:
            print(status, file=sys.stderr)
        audio_q.put(indata.copy().flatten())

    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='int16',
                            blocksize=CHUNK_SIZE, callback=audio_callback):
            while True:
                # Get audio chunk
                chunk = audio_q.get()
                
                # Predict
                # Note: openwakeword expects int16 or float32. We used int16 in sd.InputStream.
                prediction = oww_model.predict(chunk)
                score = prediction[model_key]

                # Progress bar style visualization
                bar_len = int(score * 40)
                bar = "█" * bar_len + "-" * (40 - bar_len)
                
                if score >= THRESHOLD:
                    print(f"\r[{bar}] SCORE: {score:.4f}  <-- DETECTED! ", end="", flush=True)
                    # Simple pulse effect
                    time.sleep(0.1)
                else:
                    print(f"\r[{bar}] SCORE: {score:.4f} ", end="", flush=True)

    except KeyboardInterrupt:
        print("\n\nTest stopped by user.")
    except Exception as e:
        print(f"\nError during streaming: {e}")

if __name__ == "__main__":
    test_wakeword()
