from pydantic import BaseModel, Field
from enum import Enum
from typing import Optional, Dict, Any

class ActionType(Enum):
    NAVIGATE = "navigate"
    SPEAK = "speak"
    GESTURE = "gesture"
    FETCH = "fetch"
    ESCORT = "escort"
    QUERY = "query"

class NLPActionPayload(BaseModel):
    """The handshake schema between NLP and Robotics."""
    action_type: ActionType
    params: Dict[str, Any] = Field(default_factory=dict, description="e.g. room_id, item, gesture_name")
    priority: int = Field(default=3, ge=1, le=5, description="1=urgent, 3=normal")
    utterance_id: Optional[str] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

class Utterance(BaseModel):
    """Schema for transcribed speech and metadata."""
    text: str
    language: str
    confidence: float
    timestamp: float
    id: str

class DialogueState(BaseModel):
    """Schema for tracking conversation state."""
    session_id: str
    last_utterance: Optional[Utterance] = None
    history: list[Dict[str, str]] = Field(default_factory=list)
    active_entities: Dict[str, Any] = Field(default_factory=dict)
    current_intent: Optional[str] = None
