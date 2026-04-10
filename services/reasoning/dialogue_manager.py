import re
import asyncio
from typing import TypedDict, List, Dict, Any, Awaitable, Callable, Optional
from core.interfaces import ILLMProvider
from core.schemas import Utterance, NLPActionPayload

# Define the State for LangGraph-style flow
class AgentState(TypedDict):
    """The internal state of the dialogue agent."""
    messages: List[Dict[str, str]]
    context: Dict[str, Any]
    next_step: str
    extracted_actions: List[NLPActionPayload]
    response_text: str

from services.memory.memory_manager import PersonasMemory
from tools.mcp_calendar import CalendarMCPTool
from services.integration.vms_service import VMSService

class DialogueManager:
    """
    Manages multi-turn conversation state and logic.
    Integrated with ChromaDB for personas, MCP for calendar, and VMS bridge.
    """
    def __init__(self, llm_provider: ILLMProvider):
        self.llm = llm_provider
        self.memory = PersonasMemory()
        self.calendar = CalendarMCPTool()
        self.vms = VMSService()
        self.sessions: Dict[str, AgentState] = {}

    def get_or_create_session(self, session_id: str) -> AgentState:
        if session_id not in self.sessions:
            self.sessions[session_id] = {
                "messages": [],
                "context": {},
                "next_step": "start",
                "extracted_actions": [],
                "response_text": ""
            }
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
            response_content = self._handle_simple_query(utterance.text, state)
            state["messages"].append({"role": "user", "content": utterance.text})
            state["messages"].append({"role": "assistant", "content": response_content})
            state["response_text"] = response_content
            state["extracted_actions"] = []
            return state
        
        # 2. Personalization: Inject Persona Profile if recognized (async)
        person_id = state["context"].get("recognized_person_id")
        if person_id:
            profile = await self.memory.get_persona_async(person_id)
            if profile:
                state["context"]["person_profile"] = profile

        # 3. Add user message to history
        state["messages"].append({"role": "user", "content": utterance.text})
        state["context"]["language"] = utterance.language
        state["context"]["schedule_query"] = self._is_schedule_query(utterance.text)
        state["context"]["navigation_query"] = self._is_navigation_query(utterance.text)
        
        # 4. OPTIMIZATION: Pre-fetch all tool results BEFORE LLM call (NO second pass)
        tool_results = {}
        if state["context"]["schedule_query"]:
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
        if "[ACTION: CHECK_VISITOR]" in response_content:
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
        
        # 8. Update State
        state["response_text"] = self._strip_actions(response_content)
        state["extracted_actions"] = actions
        state["messages"].append({"role": "assistant", "content": state["response_text"]})
        
        # OPTIMIZATION: Reduce context window from 10 to 5 turns for faster processing
        if len(state["messages"]) > 5:
            state["messages"] = state["messages"][-5:]
            
        return state

    def _build_prompt(self, state: AgentState, tool_results: Dict[str, str] = None) -> str:
        """
        Constructs the final system prompt with persona and context injection.
        OPTIMIZATION: Tool results are injected directly (no second pass needed).
        """
        if tool_results is None:
            tool_results = {}
            
        history_str = "\n".join([f"{m['role']}: {m['content']}" for m in state["messages"]])
        
        # Identity injection
        persona = state["context"].get("person_profile", {})
        if persona:
            identity_snip = (
                f"User: {persona.get('name', 'Unknown')} | "
                f"Role: {persona.get('role', 'Guest')} | "
                f"Pref: {persona.get('pref', 'None')}"
            )
        else:
            identity_snip = "User: Unknown | Role: Guest | Pref: None"
        
        # Inject tool results into context if available
        tool_context = ""
        if tool_results.get("calendar"):
            tool_context = f"\n[CALENDAR INFORMATION]\n{tool_results['calendar']}"
        
        return f"""
[SYSTEM CONTEXT]
{identity_snip}{tool_context}
Detected Language: {state['context'].get('language', 'English')} — reply in this language.

[CONVERSATION HISTORY]
{history_str}

[INSTRUCTIONS]
You are Jarvis, a friendly, efficient humanoid robot assistant.
1. Always reply in the SAME LANGUAGE the user spoke. If they spoke Hindi, reply in Hindi. If English, reply in English.
2. Answer the user's actual question directly. Do not add unrelated personal schedule, visitor, or calendar information.
3. Greet the user by name only if they were recognized and the user is greeting you.
4. If calendar information is provided above and user asks about schedule, use it directly.
5. If the user asks to move, go, navigate, escort, or take them somewhere, include exactly one navigation action tag: [ACTION: NAVIGATE {{"destination": "PLACE_NAME"}}].
6. Include a gesture tag only when it is natural: [ACTION: GESTURE {{"type": "GESTURE_ID"}}].
7. Available Gestures: GREET_WAVE, POINT_ROOM, NOD_HEAD, SHRUG.
8. Keep spoken answers concise, natural, and useful. Do not mention internal tags or tools aloud.

[RESPONSE]
"""

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
                visible_sentence = self._strip_actions(sentence).replace("[TOOL: get_calendar_schedule]", "").strip().strip('"')
                if visible_sentence:
                    await on_response_sentence(visible_sentence)

        print()

        if on_response_sentence:
            tail = self._strip_actions(sentence_buffer).replace("[TOOL: get_calendar_schedule]", "").strip().strip('"')
            if tail:
                await on_response_sentence(tail)

        return response_content

    def _strip_actions(self, text: str) -> str:
        """Removes the [ACTION: ...] tags from the text shown to the user."""
        import re
        return re.sub(r"\[ACTION:\s*.*?\]", "", text).strip()

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
        OPTIMIZATION: Detect simple queries that bypass the LLM entirely.
        Handles both English and Hindi common phrases.
        """
        lowered = text.lower().strip()

        # Time queries — English
        time_patterns = ["what time", "what's the time", "tell me the time", "current time"]
        if any(p in lowered for p in time_patterns):
            return True

        # Time queries — Hindi
        hindi_time = ["kya time", "kya samay", "kitne baje", "time kya", "samay kya"]
        if any(p in lowered for p in hindi_time):
            return True

        # Greetings — English (exact match)
        en_greetings = {"hello", "hi there", "hey", "good morning", "good afternoon", "good evening"}
        if lowered in en_greetings:
            return True

        # Greetings — Hindi
        hi_greetings = {"namaste", "namaskar", "helo", "hi", "hello"}
        if lowered in hi_greetings:
            return True

        # Acknowledgements — English
        en_acks = {"thanks", "thank you", "ok", "okay", "yes", "no", "nope", "got it", "alright"}
        if lowered in en_acks:
            return True

        # Acknowledgements — Hindi
        hi_acks = {"shukriya", "dhanyavaad", "dhanyawad", "theek hai", "theek", "haan", "nahi", "nah",
                   "acha", "accha", "thik hai", "bilkul", "zaroor"}
        if lowered in hi_acks:
            return True

        return False
    
    def _handle_simple_query(self, text: str, state: AgentState) -> str:
        """
        OPTIMIZATION: Handle simple queries without LLM — English and Hindi.
        """
        lowered = text.lower().strip()
        import datetime
        lang = state["context"].get("language", "English")
        is_hindi = "hindi" in lang.lower() or any(
            w in lowered for w in ["namaste", "namaskar", "shukriya", "dhanyavaad",
                                    "theek hai", "haan", "nahi", "acha", "kya time",
                                    "kitne baje", "samay"]
        )

        # Time queries
        time_patterns_en = ["what time", "what's the time", "tell me the time", "current time"]
        time_patterns_hi = ["kya time", "kya samay", "kitne baje", "time kya", "samay kya"]
        if any(p in lowered for p in time_patterns_en + time_patterns_hi):
            now = datetime.datetime.now()
            if is_hindi:
                return f"Abhi {now.strftime('%I:%M %p')} baje hain."
            return f"It is currently {now.strftime('%I:%M %p')}."

        # English greetings
        name = state["context"].get("person_profile", {}).get("name")
        if lowered in {"hello", "hi there", "hey"}:
            return f"Hello {name}! How can I help?" if name else "Hello! How can I help you today?"
        if lowered in {"good morning", "good afternoon", "good evening"}:
            return "Good to see you! What can I do for you?"

        # Hindi greetings
        if lowered in {"namaste", "namaskar"}:
            if name:
                return f"Namaste {name}! Main aapki kya madad kar sakta hoon?"
            return "Namaste! Main aapki kya madad kar sakta hoon?"

        # English acknowledgements
        if lowered in {"thanks", "thank you"}:
            return "You're welcome!"
        if lowered in {"ok", "okay", "got it", "alright"}:
            return "Got it."
        if lowered == "yes":
            return "Great!"
        if lowered in {"no", "nope"}:
            return "No problem."

        # Hindi acknowledgements
        if lowered in {"shukriya", "dhanyavaad", "dhanyawad"}:
            return "Koi baat nahi!"
        if lowered in {"theek hai", "theek", "thik hai", "acha", "accha"}:
            return "Theek hai!"
        if lowered in {"haan", "bilkul", "zaroor"}:
            return "Bahut accha!"
        if lowered in {"nahi", "nah"}:
            return "Koi baat nahi."

        return "I'm ready to help."

    def update_context(self, session_id: str, new_context: Dict[str, Any]):
        state = self.get_or_create_session(session_id)
        state["context"].update(new_context)
