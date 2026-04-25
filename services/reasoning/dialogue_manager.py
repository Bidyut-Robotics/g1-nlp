import re
import asyncio
import datetime
from typing import TypedDict, List, Dict, Any, Awaitable, Callable, Optional
from core.interfaces import ILLMProvider
from core.schemas import Utterance, NLPActionPayload, ActionType
from core.config import load_app_config

# ─── Week 1 feature flags (set to True to re-enable in Week 2+) ──────────────
WEEK1_PERSONA_ENABLED = False
WEEK1_CALENDAR_ENABLED = False
WEEK1_VMS_ENABLED = False
WEEK1_NAVIGATION_ENABLED = False

# Define the State for LangGraph-style flow
class AgentState(TypedDict):
    """The internal state of the dialogue agent."""
    messages: List[Dict[str, str]]
    context: Dict[str, Any]
    next_step: str
    extracted_actions: List[NLPActionPayload]
    response_text: str
    exit_intent: bool   # True when user wants to end the conversation

if WEEK1_PERSONA_ENABLED:
    from services.memory.memory_manager import PersonasMemory
if WEEK1_CALENDAR_ENABLED:
    from tools.mcp_calendar import CalendarMCPTool
if WEEK1_VMS_ENABLED:
    from services.integration.vms_service import VMSService

class DialogueManager:
    """
    Manages multi-turn conversation state and logic.
    Integrated with ChromaDB for personas, MCP for calendar, and VMS bridge.
    """
    def __init__(self, llm_provider: ILLMProvider):
        self.llm = llm_provider
        self.memory = PersonasMemory() if WEEK1_PERSONA_ENABLED else None
        self.calendar = CalendarMCPTool() if WEEK1_CALENDAR_ENABLED else None
        self.vms = VMSService() if WEEK1_VMS_ENABLED else None
        self.sessions: Dict[str, AgentState] = {}

        # Load robot factual identity from config (used by fast-path handler)
        app_cfg = load_app_config()
        ri = app_cfg.get("robot_info", {})
        self.robot_name = ri.get("name", "Jarvis")
        self.robot_company = ri.get("company", "this company")
        self.robot_location = ri.get("location", "this office")
        self.robot_role = ri.get("role", "office assistant robot")

    def get_or_create_session(self, session_id: str) -> AgentState:
        if session_id not in self.sessions:
            self.sessions[session_id] = {
                "messages": [],
                "context": {},
                "next_step": "start",
                "extracted_actions": [],
                "response_text": "",
                "exit_intent": False,
            }
        else:
            # Reset exit_intent at the start of each turn
            self.sessions[session_id]["exit_intent"] = False
        return self.sessions[session_id]

    async def process_utterance(
        self,
        session_id: str,
        utterance: Utterance,
        on_response_sentence: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> AgentState:
        """
        Main entry point for processing a new user speech input.
        Returns the updated agent state including response and actions.
        OPTIMIZED: Single-pass reasoning with NO sequential LLM calls.
        """
        state = self.get_or_create_session(session_id)
        
        # 1. FAST PATH: Check for simple queries (bypass LLM entirely)
        if self._is_simple_query(utterance.text):
            state["extracted_actions"] = []   # handler may append gesture actions
            response_content = self._handle_simple_query(utterance.text, state)
            state["messages"].append({"role": "user", "content": utterance.text})
            state["messages"].append({"role": "assistant", "content": response_content})
            state["response_text"] = response_content
            # exit_intent and extracted_actions set inside _handle_simple_query
            return state
        
        # 2. Personalization: Inject Persona Profile if recognized (async)
        if WEEK1_PERSONA_ENABLED and self.memory:
            person_id = state["context"].get("recognized_person_id")
            if person_id:
                profile = await self.memory.get_persona_async(person_id)
                if profile:
                    state["context"]["person_profile"] = profile

        # 3. Add user message to history
        state["messages"].append({"role": "user", "content": utterance.text})
        state["context"]["language"] = utterance.language
        state["context"]["schedule_query"] = self._is_schedule_query(utterance.text) if WEEK1_CALENDAR_ENABLED else False
        state["context"]["navigation_query"] = self._is_navigation_query(utterance.text) if WEEK1_NAVIGATION_ENABLED else False
        
        # 4. OPTIMIZATION: Pre-fetch all tool results BEFORE LLM call (NO second pass)
        tool_results = {}
        if WEEK1_CALENDAR_ENABLED and state["context"]["schedule_query"] and self.calendar:
            name = state["context"].get("person_profile", {}).get("name", "the user")
            try:
                schedule_text = await self.calendar.get_schedule(name)
                tool_results["calendar"] = schedule_text
                print(f"[SYSTEM] Calendar prefetched for {name}")
            except Exception as e:
                print(f"[SYSTEM] Calendar lookup failed: {e}")
        
        # 5. SINGLE LLM PASS: Build prompt with all context + tool results injected upfront
        prompt = self._build_prompt(state, tool_results)
        response_content = await self._stream_response(
            prompt,
            state["context"],
            log_prefix="\n[AI Thinking]: ",
            on_response_sentence=on_response_sentence,
        )

        # 6. VMS Integration (no second LLM pass)
        if WEEK1_VMS_ENABLED and self.vms and "[ACTION: CHECK_VISITOR]" in response_content:
            name = re.search(r"CHECK_VISITOR\s*\{\s*'name':\s*'(.*?)'", response_content)
            if name:
                try:
                    v_info = self.vms.lookup_visitor(name.group(1))
                    state["context"]["visitor_info"] = v_info
                    if v_info.get("status") == "expected":
                        self.vms.register_arrival(v_info["id"])
                except Exception as e:
                    print(f"[SYSTEM] VMS lookup failed: {e}")

        # 7. Action Extraction (Gestures, Navigation)
        actions = await self.llm.extract_actions(response_content)
        gesture_actions = self._extract_gesture_actions(response_content)

        # 8. Update State
        state["response_text"] = self._strip_tags(response_content)
        state["extracted_actions"] = actions + gesture_actions
        state["messages"].append({"role": "assistant", "content": state["response_text"]})
        
        # OPTIMIZATION: Reduce context window from 10 to 5 turns for faster processing
        if len(state["messages"]) > 5:
            state["messages"] = state["messages"][-5:]
            
        return state

    def _build_prompt(self, state: AgentState, tool_results: Dict[str, str] = None) -> str:
        if tool_results is None:
            tool_results = {}

        # Extract the last user message to give the model explicit focus
        user_question = ""
        for m in reversed(state["messages"]):
            if m["role"] == "user":
                user_question = m["content"]
                break

        # Build conversation history (excluding the last user turn — shown separately)
        history_lines = []
        pending_user = True  # skip the last user message from history
        for m in reversed(state["messages"]):
            if pending_user and m["role"] == "user":
                pending_user = False
                continue
            history_lines.insert(0, f"{m['role'].upper()}: {m['content']}")
        history_str = "\n".join(history_lines) if history_lines else "(new conversation)"

        # Build context section
        context_parts = []
        if WEEK1_PERSONA_ENABLED:
            persona = state["context"].get("person_profile", {})
            if persona:
                context_parts.append(
                    f"User: {persona.get('name', 'Unknown')} | "
                    f"Role: {persona.get('role', 'Guest')} | "
                    f"Pref: {persona.get('pref', 'None')}"
                )
        if WEEK1_CALENDAR_ENABLED and tool_results.get("calendar"):
            context_parts.append(f"\n[CALENDAR INFORMATION]\n{tool_results['calendar']}")

        context_block = "\n".join(context_parts) if context_parts else ""

        return f"""You are Jarvis, a helpful humanoid robot assistant. Answer questions accurately and concisely.

FACTS ABOUT YOU (always use these, never contradict them):
- Your name is {self.robot_name}
- You were built by {self.robot_company}
- You are deployed at: {self.robot_location}
- Your role: {self.robot_role}

RULES:
- Answer the CURRENT QUESTION directly. Do not repeat greetings.
- Give a specific, factual answer in 1-3 sentences.
- Do not start with "Hello", "Hi", "Certainly", "Of course", or filler phrases.
- Do not make up facts, distances, place names, or technical specifications.
- If uncertain, say 'I'm not sure about that' or 'I don't have that information'.
- Speak naturally, as if talking to a person face-to-face.


{context_block}
CONVERSATION HISTORY:
{history_str}

CURRENT QUESTION: {user_question}

ANSWER:"""



    async def _stream_response(
        self,
        prompt: str,
        context: Dict[str, Any],
        log_prefix: str,
        on_response_sentence: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> str:
        response_content = ""
        sentence_buffer = ""

        print(log_prefix, end="", flush=True)
        async for chunk in self.llm.generate_response(prompt, context=context):
            print(chunk, end="", flush=True)
            response_content += chunk
            sentence_buffer += chunk

            if not on_response_sentence:
                continue

            while True:
                match = re.search(r"(.+?[.!?])(?:\s+|$)", sentence_buffer, re.DOTALL)
                if not match:
                    break

                sentence = match.group(1).strip()
                sentence_buffer = sentence_buffer[match.end():]
                visible_sentence = self._strip_tags(sentence).replace("[TOOL: get_calendar_schedule]", "").strip().strip('"')
                if visible_sentence:
                    await on_response_sentence(visible_sentence)

        print()

        if on_response_sentence:
            tail = self._strip_tags(sentence_buffer).replace("[TOOL: get_calendar_schedule]", "").strip().strip('"')
            if tail:
                await on_response_sentence(tail)

        return response_content

    def _strip_actions(self, text: str) -> str:
        """Removes [ACTION: ...] tags from text shown to the user."""
        return re.sub(r"\[ACTION:\s*.*?\]", "", text).strip()

    def _strip_tags(self, text: str) -> str:
        """Removes both [ACTION: ...] and [GESTURE: ...] tags from visible text."""
        text = re.sub(r"\[ACTION:\s*.*?\]", "", text)
        text = re.sub(r"\[GESTURE:\s*\w+\]", "", text)
        return text.strip()

    def _extract_gesture_actions(self, text: str) -> list:
        """
        Parse [GESTURE: gesture_name] tags emitted by the LLM and return
        NLPActionPayload objects so the pipeline can execute them.
        """
        actions = []
        for match in re.finditer(r"\[GESTURE:\s*(\w+)\]", text):
            gesture_name = match.group(1).lower()
            actions.append(
                NLPActionPayload(
                    action_type=ActionType.GESTURE,
                    params={"gesture_name": gesture_name},
                )
            )
        return actions

    def _is_schedule_query(self, text: str) -> bool:
        lowered = text.lower()
        keywords = [
            "schedule",
            "calendar",
            "meeting",
            "meetings",
            "appointment",
            "appointments",
            "available",
            "availability",
            "agenda",
        ]
        return any(keyword in lowered for keyword in keywords)

    def _is_navigation_query(self, text: str) -> bool:
        lowered = text.lower()
        keywords = ["move", "go to", "navigate", "escort", "take me", "lead me", "bring me"]
        return any(keyword in lowered for keyword in keywords)

    def _is_simple_query(self, text: str) -> bool:
        """
        Detect simple factual queries that bypass the LLM entirely.
        """
        lowered = text.lower().strip()

        fast_patterns = [
            # Time
            "what time", "what's the time", "tell me the time", "current time",
            # Date
            "what date", "what's the date", "today's date", "what day is it",
            "what day", "what is today", "what's today",
            # Location / where
            "where are you", "where are we", "where we are", "where am i",
            "which office", "what office", "where is this", "where do you work",
            "your location", "our location", "what is this place", "what place is this",
            # Company / organization
            "which company", "what company", "who made you", "who built you",
            "who created you", "which organization", "what organization",
            "who do you work for", "who are you built by",
            # Identity
            "who are you", "what are you", "what's your name", "what is your name",
            "your name", "who is jarvis", "tell me about yourself",
            # Help
            "what can you do", "help me", "help", "what do you do", "your capabilities",
            # Stop
            "stop", "never mind", "nevermind", "cancel", "dismiss",
            "quit", "exit", "bye", "goodbye", "see you",
            # Handshake
            "shake hand", "shake my hand", "handshake", "want to shake",
            "can you shake",
        ]
        if any(p in lowered for p in fast_patterns):
            return True

        # Greetings / acks (exact match)
        exact_matches = {
            "hello", "hi there", "hey", "good morning", "good afternoon", "good evening",
            "thanks", "thank you", "ok", "okay", "yes", "no", "nope", "got it", "alright",
        }
        if lowered in exact_matches:
            return True

        return False
    
    def _handle_simple_query(self, text: str, state: AgentState) -> str:
        """Handle simple factual queries without LLM."""
        lowered = text.lower().strip()
        now = datetime.datetime.now()

        # Time
        if any(p in lowered for p in ["what time", "what's the time", "tell me the time", "current time"]):
            return f"It is {now.strftime('%I:%M %p')}."

        # Date
        if any(p in lowered for p in ["what date", "what's the date", "today's date", "what day is it", "what day", "what is today", "what's today"]):
            return f"Today is {now.strftime('%A, %d %B %Y')}."

        # Location
        if any(p in lowered for p in ["where are you", "where are we", "where we are", "where am i", "which office", "what office", "where is this", "where do you work", "your location", "our location", "what is this place", "what place is this"]):
            return f"We are at {self.robot_location}."

        # Company / creator
        if any(p in lowered for p in ["which company", "what company", "who made you", "who built you", "who created you", "which organization", "what organization", "who do you work for", "who are you built by"]):
            return f"I was built by {self.robot_company}."

        # Identity
        if any(p in lowered for p in ["who are you", "what are you", "what's your name", "what is your name", "your name", "who is jarvis", "tell me about yourself"]):
            return f"I'm {self.robot_name}, a {self.robot_role} built by {self.robot_company}."

        # Help / capabilities
        if any(p in lowered for p in ["what can you do", "help me", "help", "what do you do", "your capabilities"]):
            return f"I can answer your questions, tell you the time and date, tell you about this office, and have a conversation with you."

        # Stop / dismiss — set exit_intent so the main loop can break
        if any(p in lowered for p in ["stop", "never mind", "nevermind", "cancel", "dismiss", "quit", "exit", "bye", "goodbye", "see you"]):
            state["exit_intent"] = True
            state["extracted_actions"].append(
                NLPActionPayload(action_type=ActionType.GESTURE, params={"gesture_name": "wave_goodbye"})
            )
            return "Alright, goodbye! I'll be here if you need anything."

        # Handshake (explicit request)
        if any(p in lowered for p in ["shake hand", "shake my hand", "handshake", "want to shake", "can you shake"]):
            state["extracted_actions"].append(
                NLPActionPayload(action_type=ActionType.GESTURE, params={"gesture_name": "shake_hand"})
            )
            return "Of course! Please extend your hand."

        # Greetings
        if lowered in {"hello", "hi there", "hey"}:
            state["extracted_actions"].append(
                NLPActionPayload(action_type=ActionType.GESTURE, params={"gesture_name": "wave_hello"})
            )
            return f"Hello! How can I help you?"
        if "good morning" in lowered:
            state["extracted_actions"].append(
                NLPActionPayload(action_type=ActionType.GESTURE, params={"gesture_name": "wave_hello"})
            )
            return "Good morning! What can I do for you?"
        if "good afternoon" in lowered:
            state["extracted_actions"].append(
                NLPActionPayload(action_type=ActionType.GESTURE, params={"gesture_name": "wave_hello"})
            )
            return "Good afternoon! How can I help?"
        if "good evening" in lowered:
            state["extracted_actions"].append(
                NLPActionPayload(action_type=ActionType.GESTURE, params={"gesture_name": "wave_hello"})
            )
            return "Good evening! What do you need?"

        # Acknowledgements
        if lowered in {"thanks", "thank you"}:
            state["extracted_actions"].append(
                NLPActionPayload(action_type=ActionType.GESTURE, params={"gesture_name": "bow"})
            )
            return "You're welcome!"
        if lowered in {"ok", "okay", "got it", "alright"}:
            return "Got it."
        if lowered == "yes":
            return "Sure!"
        if lowered in {"no", "nope"}:
            return "Understood."

        return "Got it."

    def update_context(self, session_id: str, new_context: Dict[str, Any]):
        state = self.get_or_create_session(session_id)
        state["context"].update(new_context)
