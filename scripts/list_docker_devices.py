import sounddevice as sd
import os

print("--- Environment ---")
print(f"PULSE_SERVER: {os.getenv('PULSE_SERVER')}")
print(f"PULSE_SOURCE: {os.getenv('PULSE_SOURCE')}")
print(f"PULSE_SINK: {os.getenv('PULSE_SINK')}")

print("\n--- Sounddevice Devices ---")
try:
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        print(f"{i}: {d['name']} (in={d['max_input_channels']}, out={d['max_output_channels']})")
    
    default_input = sd.default.device[0]
    print(f"\nDefault Input: {default_input} -> {devices[default_input]['name'] if default_input is not None else 'None'}")
except Exception as e:
    print(f"Error listing devices: {e}")
