"""
Parakeet v2 ASR accuracy test using NeMo — records from mic and transcribes.
Run: python3 test_parakeet.py
Press Enter to start recording, speak, press Enter again to stop.
"""

import threading
import numpy as np
import sounddevice as sd
import nemo.collections.asr as nemo_asr

MODEL_NAME  = "nvidia/parakeet-tdt-0.6b-v2"
SAMPLE_RATE = 16000

print(f"Loading {MODEL_NAME} via NeMo...")
model = nemo_asr.models.ASRModel.from_pretrained(model_name=MODEL_NAME)
model.eval()
print("Model loaded. Press Enter to start recording, Enter again to stop.\n")

try:
    while True:
        input(">> Press Enter to START recording...")

        frames = []

        def callback(indata, frame_count, time_info, status):
            frames.append(indata.copy())

        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                dtype="float32", callback=callback)
        stream.start()
        input("   Recording... Press Enter to STOP.")
        stream.stop()
        stream.close()

        audio = np.concatenate(frames).squeeze()
        duration = len(audio) / SAMPLE_RATE
        print(f"   Recorded {duration:.1f}s — transcribing...")

        # NeMo expects a file or numpy array at 16kHz float32
        output = model.transcribe([audio], batch_size=1)
        text = output[0].text if hasattr(output[0], 'text') else str(output[0])
        print(f"\n   TRANSCRIPT: {text}\n")

except KeyboardInterrupt:
    print("\nDone.")
