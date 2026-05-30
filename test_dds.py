import sys
import time
print("Testing DDS Init...")
try:
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
    interface = sys.argv[1] if len(sys.argv) > 1 else "eth0"
    print(f"Calling ChannelFactoryInitialize(0, '{interface}')")
    ChannelFactoryInitialize(0, interface)
    print("ChannelFactoryInitialize success!")
    time.sleep(1)
    
    print("Creating AudioClient...")
    audio = AudioClient()
    print("Calling audio.Init()...")
    audio.Init()
    print("Calling audio.SetVolume(100)...")
    audio.SetVolume(100)
    print("All DDS Init success!")
except Exception as e:
    print("Exception:", e)
