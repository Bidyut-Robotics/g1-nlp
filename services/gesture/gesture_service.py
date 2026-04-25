import asyncio
import time as _time
from typing import Optional

# Logical gesture name → human-readable description
GESTURE_CATALOG = {
    "wave_hello":    "Wave hand (facing user)",
    "wave_goodbye":  "Wave hand and turn around",
    "shake_hand":    "Extend hand twice with 3s pause",
    "bow":           "Lower then return to upright",
    "attention":     "Stand upright (attention posture)",
}


class GestureService:
    """
    Executes physical gestures on the Unitree G1 via LocoClient.
    On laptop/stub mode (enabled=False) it only prints the gesture name.
    All blocking SDK calls run in asyncio.to_thread so the event loop
    stays free during gesture execution.
    """

    def __init__(self, interface: str = "eth0", enabled: bool = True):
        self.enabled = enabled
        self._client = None

        if not enabled:
            print("[GESTURE] Stub mode — gestures will be logged only.")
            return

        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

            print(f"[GESTURE] Initializing LocoClient on {interface}…")
            try:
                ChannelFactoryInitialize(0, interface)
            except Exception:
                pass  # Already initialized by TTS service — safe to continue

            self._client = LocoClient()
            self._client.SetTimeout(10.0)
            self._client.Init()
            print("[GESTURE] LocoClient ready.")
        except Exception as e:
            print(f"[GESTURE ERROR] LocoClient init failed: {e}")
            self._client = None

    # ─────────────────────────────────────────────────────────────────────────
    # Blocking SDK calls — always run via asyncio.to_thread
    # ─────────────────────────────────────────────────────────────────────────

    def _run_gesture(self, gesture_name: str) -> None:
        c = self._client

        if c is None:
            print(f"[GESTURE:STUB] {gesture_name}")
            return

        print(f"[GESTURE] ▶ {gesture_name}")

        if gesture_name == "wave_hello":
            c.WaveHand()

        elif gesture_name == "wave_goodbye":
            c.WaveHand(True)          # turns around and waves

        elif gesture_name == "shake_hand":
            c.ShakeHand()
            _time.sleep(3)            # hold position while user completes handshake
            c.ShakeHand()             # retract

        elif gesture_name == "bow":
            c.LowStand()
            _time.sleep(1)
            c.HighStand()

        elif gesture_name == "attention":
            c.HighStand()

        else:
            print(f"[GESTURE] Unknown gesture: '{gesture_name}' — ignored.")

        print(f"[GESTURE] ✓ {gesture_name} done.")

    # ─────────────────────────────────────────────────────────────────────────
    # Public async API
    # ─────────────────────────────────────────────────────────────────────────

    async def execute(self, gesture_name: str) -> None:
        """
        Execute a named gesture without blocking the event loop.
        Safe to fire-and-forget via asyncio.create_task().
        """
        if not self.enabled and self._client is None:
            print(f"[GESTURE:STUB] {gesture_name}")
            return
        try:
            await asyncio.to_thread(self._run_gesture, gesture_name)
        except Exception as e:
            print(f"[GESTURE ERROR] {gesture_name} failed: {e}")

    def supported(self) -> list:
        return list(GESTURE_CATALOG.keys())
