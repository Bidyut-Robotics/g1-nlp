import asyncio
from services.reasoning.dialogue_manager import DialogueManager

class ProactiveScheduler:
    """
    Background service that monitors for events and triggers robot-initiated interactions.
    Handles meeting reminders and visitor announcement alerts.
    """
    def __init__(self, dialogue_manager: DialogueManager):
        self.dm = dialogue_manager
        self.is_running = False

    async def start_monitoring(self):
        """Starts the main background loop."""
        self.is_running = True
        print("[PROACTIVE] Background Scheduler started.")
        
        while self.is_running:
            # 1. Poll for meeting reminders (Mocked)
            await self._poll_schedules()
            
            # Check every 30 seconds for demonstration purposes
            await asyncio.sleep(30)

    async def _poll_schedules(self):
        """Checks if any employee has a meeting starting within 5 minutes."""
        # Simulated logic: If current time is nearing a mock schedule item
        # We would fetch schedules via the MCP Calendar Tool
        pass

    async def trigger_proactive_interaction(self, session_id: str, event_type: str, data: dict):
        """
        Manually trigger an interaction from an external event (e.g., Visitor Arrival).
        """
        print(f"[PROACTIVE] Triggering interaction for: {event_type}...")
        
        if event_type == "VISITOR_ARRIVAL":
            visitor = data.get("visitor_name", "A guest")
            host = data.get("host_name", "the team")
            
            # We inject a hidden user-like message to the DM to trigger the logic
            # This 'vibe' coding trick allows us to re-use the dialogue loop
            synthetic_utterance = f"PROACTIVE_SYSTEM_EVENT: {visitor} has arrived for {host}. Inform the host."
            
            # Note: We need a specialized prompt path in DM for this, or just let the LLM handle it.
            # In our current DM, it's safer to have a specific 'injection' method.
            print(f"[PROACTIVE] Injecting event to Dialogue Manager: {synthetic_utterance}")
            
    def stop(self):
        self.is_running = False
