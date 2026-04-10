import asyncio
import os
import sys
from core.factory import ServiceFactory
from services.reasoning.dialogue_manager import DialogueManager
from core.schemas import Utterance
import time

async def test_integrated_pipeline():
    print("--- [SMOKE TEST] Humanoid NLP Pipeline ---")
    
    # 1. Check for models directory
    if not os.path.exists("models"):
        os.makedirs("models")
        print("[!] Warning: 'models' directory created. Ensure Piper and VAD models are placed here.")

    # 2. Initialize Dialogue Manager
    # Note: This will attempt to connect to Ollama (default: http://localhost:11434)
    try:
        llm = ServiceFactory.get_llm_provider()
        dm = DialogueManager(llm)
        print("[OK] DialogueManager initialized.")
    except Exception as e:
        print(f"[ERROR] Failed to initialize DialogueManager: {e}")
        return

    # 3. Simulate Recognition Event (Persona Lookup)
    print("\n[STEP 1] Simulating 'Surya' identification...")
    from scripts.seed_personas import seed
    seed() # Ensure DB has data
    
    session_id = "test_session_001"
    dm.update_context(session_id, {"recognized_person_id": "emp_001"})

    # 4. Process a Mock Utterance
    print("\n[STEP 2] Processing utterance: 'Hi, what's my schedule today?'")
    test_utterance = Utterance(
        text="Hi, what's my schedule today?",
        language="English",
        confidence=1.0,
        timestamp=time.time(),
        id="test_001"
    )
    
    try:
        state = await dm.process_utterance(session_id, test_utterance)
        
        print(f"\n[RESPONSE] Robot says: \"{state['response_text']}\"")
        
        # 5. Check for Actions & Tools
        if state["extracted_actions"]:
            print("\n[ACTIONS EXTRACTED]:")
            for action in state["extracted_actions"]:
                print(f" - {action.action_type.value}: {action.params}")
        
        if "schedule" in state["response_text"].lower() or "[TOOL:" in str(state):
            print("\n[OK] Pipeline successfully triggered calendar context.")
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\n[ERROR] Pipeline execution failed: {repr(e)}")
        print("Tip: Is Ollama running? Try 'ollama serve' and 'ollama pull llama3.2'")

if __name__ == "__main__":
    asyncio.run(test_integrated_pipeline())
