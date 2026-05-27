import asyncio
import json
import socket
import time as _time

GESTURE_CATALOG = {
    "wave_hello":     "Wave hand (facing user)",
    "wave_goodbye":   "Wave hand and turn around",
    "shake_hand":     "Extend hand twice with 3s pause",
    "bow":            "Lower then return to upright",
    "attention":      "Stand upright (attention posture)",
    "move_forward":   "Walk forward ~1 step then stop",
    "move_backward":  "Walk backward ~1 step then stop",
}


class GestureService:
    """
    Sends gesture commands to robot_agent (127.0.0.1:7788) via TCP.
    robot_agent runs on AGX and executes them via LocoClient over DDS.
    """

    def __init__(self, interface: str = "eth0", enabled: bool = True,
                 server_host: str = "127.0.0.1", server_port: int = 7788):
        self.enabled = enabled
        self.server_host = server_host
        self.server_port = server_port

        if not enabled:
            print("[GESTURE] Stub mode — gestures will be logged only.")
            return

        # Verify server is reachable
        try:
            with socket.create_connection((server_host, server_port), timeout=3):
                pass
            print(f"[GESTURE] Connected to gesture server at {server_host}:{server_port}")
        except Exception as e:
            print(f"[GESTURE] Warning: gesture server not reachable ({e}) — will retry on each gesture")

    def _run_gesture(self, gesture_name: str) -> None:
        if not self.enabled:
            print(f"[GESTURE:STUB] {gesture_name}")
            return

        if gesture_name in ("bow", "attention"):
            print(f"[GESTURE] '{gesture_name}' paused — not active in phase 1.")
            return

        if gesture_name not in GESTURE_CATALOG:
            print(f"[GESTURE] Unknown gesture: '{gesture_name}' — ignored.")
            return

        print(f"[GESTURE] ▶ {gesture_name}")
        try:
            with socket.create_connection((self.server_host, self.server_port), timeout=5) as s:
                s.send(json.dumps({"gesture": gesture_name}).encode())
            print(f"[GESTURE] ✓ {gesture_name} sent.")
        except Exception as e:
            print(f"[GESTURE ERROR] {gesture_name} failed: {e}")

    async def execute(self, gesture_name: str) -> None:
        try:
            await asyncio.to_thread(self._run_gesture, gesture_name)
        except Exception as e:
            print(f"[GESTURE ERROR] {gesture_name} failed: {e}")

    def supported(self) -> list:
        return list(GESTURE_CATALOG.keys())
