import asyncio
from typing import Callable, Awaitable, Dict

class GestureDispatcher:
    """
    Interfaces with ROS 2 Action Servers for robot gestures.
    Coordinates speech onset with physical movement (e.g., waiting for 'Peak Visibility').
    """
    def __init__(self):
        # Internal mapping of NLP intent to ROS 2 Action goal types
        self.gesture_library = {
            "GREET_WAVE": {"action": "wave_hand", "peak_ms": 300},
            "POINT_ROOM": {"action": "point_arm", "peak_ms": 600},
            "NOD_HEAD": {"action": "nod", "peak_ms": 200},
            "SHRUG": {"action": "shrug_shoulders", "peak_ms": 400}
        }

    async def dispatch_with_sync(self, gesture_id: str, speech_callback: Callable[[], Awaitable[None]]):
        """
        Triggers a ROS 2 Action and starts the speech callback at the 'Peak' of the motion.
        """
        gesture = self.gesture_library.get(gesture_id, self.gesture_library["GREET_WAVE"])
        action_name = gesture["action"]
        peak_delay = gesture["peak_ms"] / 1000.0
        
        print(f"[ROS2 ACTION] Starting: {action_name}...")
        
        # 1. Wait for physical 'Peak' before starting TTS
        # In a real ROS2 implementation, we would listen for a feedback msg
        await asyncio.sleep(peak_delay)
        
        print(f"[ROS2 ACTION] Peak reached for {action_name}. Syncing speech...")
        await speech_callback()
        
        # 2. Wait for Action Completion
        await asyncio.sleep(1.0) # Mock time for total motion duration
        print(f"[ROS2 ACTION] {action_name} sequence complete.")

    def get_supported_gestures(self) -> Dict[str, str]:
        return {k: v["action"] for k, v in self.gesture_library.items()}
