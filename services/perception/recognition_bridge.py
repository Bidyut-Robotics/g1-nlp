from services.reasoning.dialogue_manager import DialogueManager
from typing import Dict, Any

class RecognitionBridge:
    """
    Subscribes to Perception/Vision events (Mocked as ROS 2 topics).
    Links face recognition outcomes to the NLP Dialogue context.
    """
    def __init__(self, dialogue_manager: DialogueManager):
        self.dm = dialogue_manager

    def trigger_face_event(self, session_id: str, person_id: str, confidence: float = 1.0):
        """
        Simulates an incoming face recognition event.
        Updates the dialogue context so the next LLM response is personalized.
        """
        if confidence < 0.7:
            print(f"[RECOGNITION] Low confidence ({confidence}). Ignoring face event.")
            return

        print(f"[RECOGNITION] Person identified: {person_id}. Updating Dialogue Context...")
        
        # Inject the recognized ID into the session context
        self.dm.update_context(session_id, {
            "recognized_person_id": person_id,
            "recognition_source": "perception_module_v1",
            "last_face_match_time": 0 # Replace with time.time()
        })

    def trigger_unknown_visitor(self, session_id: str):
        """Simulates detection of a person not in the employee database."""
        print("[RECOGNITION] Unknown visitor detected. Setting context to 'GUEST_MODE'.")
        self.dm.update_context(session_id, {
            "recognized_person_id": None,
            "visitor_active": True
        })
