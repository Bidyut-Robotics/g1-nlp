##How the architecture fits together

Think of the NLP module as a signal processing pipeline where raw sound goes in and structured commands come out. You own every layer except the last mile — the robotics team owns what happens after your action dispatcher fires.
The architecture has a clean separation of concerns across six layers. The perception layer (teal) is where physics becomes language — Whisper converts waveform to tokens, language ID routes it correctly, and the context injector enriches the transcript with situational awareness before anything else sees it. Getting this layer right is critical because garbage in means garbage out for everything downstream.
The understanding layer (purple) is where you extract meaning: what does the person want (intent), who or what are they talking about (entities), and how are they feeling (sentiment). These three signals together allow the reasoning layer to make smarter decisions. For instance, if the intent is "navigate" but no entity was extracted (room name is missing), the dialogue manager knows to ask a clarifying question rather than dispatch an empty action.
The reasoning layer (blue) is the cognitive core. The dialogue manager is the conductor — it holds conversation state across turns, decides when enough information has been gathered to act, and handles repair when something goes wrong. The RAG retriever and LLM tool-calling work as its two arms: one pulls grounded facts from your knowledge base, the other reaches out to live systems like Google Calendar or the VMS.
The action dispatcher is your handshake boundary with the robotics team. Define a clean Pydantic schema for every action type early — NavigateAction(room_id, priority), GestureAction(type), SpeakAction(text, language, tone) — and both teams can work in parallel without blocking each other.

What to prioritize in your 18-week plan
Phase 1 is the most important to get right architecturally, even though it's the simplest functionally. The ASR-to-TTS loop is your foundation — every latency budget decision you make here propagates upward. A practical target is under 3 seconds end-to-end for the first response (ASR processing + LLM call + TTS generation), and under 1.5 seconds for follow-on turns where you can use cached context. Push for streaming TTS from the start so the robot can begin speaking while the LLM is still generating the tail of the response — this makes interaction feel natural rather than robotic.
Phase 2's dialogue manager is where most NLP projects either succeed or quietly fail. The temptation is to skip a proper state machine and just dump everything into a single LLM prompt. That works for demos but breaks under real-world edge cases: interruptions, topic switches, mid-sentence corrections, silence timeouts. LangGraph is your friend here — it lets you define conversation states as explicit graph nodes, which makes the system debuggable and predictable.
The data strategy is something to start on week 1, not week 8. Your intent classifier and NER model will only be as good as your training data. Set up Label Studio now, start collecting real utterances from any available demos or internal testing, and build your annotation pipeline early. Even 200 annotated examples will significantly outperform zero-shot-only approaches for domain-specific intents like "escort me to meeting room 4B."

The interface contract with the robotics team
Since you're leading NLP and they own robotics, agree on this schema early and put it in a shared repo:
python

# action_schema.py — owned jointly, versioned

from pydantic import BaseModel
from enum import Enum

class ActionType(Enum):
NAVIGATE = "navigate"
SPEAK = "speak"
GESTURE = "gesture"
FETCH = "fetch"
ESCORT = "escort"

class NLPActionPayload(BaseModel):
action_type: ActionType
params: dict # room_id, item, gesture_name etc.
priority: int # 1=urgent, 3=normal
utterance_id: str # trace back to the original ASR transcript
confidence: float # NLP confidence — robotics team can reject low-confidence actions
This clean boundary means you can test your NLP module end-to-end with a mock robotics stub, and they can test their motion planner with mock NLP payloads. No dependency hell.

The latency reality
Yes, if you run every layer sequentially and naively, you'll hit 6–10 seconds end-to-end. That's unusable. But the architecture doesn't have to be sequential — here's how you get it under 2 seconds:
Parallelization is the primary weapon. ASR finishes → immediately fire intent classification AND entity extraction AND context injection in parallel. Don't wait for one to finish before starting the next. With asyncio and concurrent LLM calls, the "understanding" layer collapses from ~3 serial steps to the time of the slowest single step.
Streaming is the second weapon. Don't wait for the full LLM response before starting TTS. Stream tokens from the LLM, buffer the first sentence (~15 tokens), start speaking while the rest generates. The robot starts talking within ~800ms of the LLM beginning its response, even if the full response takes 3 seconds to generate. This is the single highest-impact optimization you can make.
VAD-triggered pre-warming. The moment Silero VAD detects speech onset, start warming the ASR buffer and pre-loading session context from Redis. By the time the person finishes speaking, context is already in memory.
Realistic latency budget with these optimizations:
Stage
Naive
Optimized
VAD detection
80ms
80ms
Whisper ASR (Jetson Orin, int8)
800ms
400ms (streaming decode)
Lang ID
10ms
10ms
Intent + entity (parallel)
600ms
200ms (GPT-4o mini)
RAG retrieval (if needed)
500ms
150ms (cached + quantized)
LLM generation (first token)
700ms
350ms (streaming)
TTS first audio chunk
300ms
180ms (streaming TTS)
Total to first audio
~3s
~1.2s

The 1.2s target is achievable in production with the right stack choices.

On-device vs. cloud — the connectivity question
This is the most architecturally important decision you'll make. Here's the honest breakdown:
V
visualize show_widget

Fully on-device
Everything on Jetson Orin
Latency<2s
Offline capabilityComplete
Voice qualityAcceptable (smaller models)
Setup complexityHigh (model optimization)
Data privacyMaximum
Monthly costNear zero
Jetson Orin NX at 100 TOPS can run Whisper + Llama 3.2-3B + Coqui. Tight on memory (16GB). LLM quality noticeably lower than GPT-4o.
Or hybrid
Hybrid
best for this project
On-device core + LAN server
Latency<1.5s (LAN only)
Offline capabilityFull core functionality
Voice qualityGood to excellent
Setup complexityMedium
Data privacyAudio stays on-premises
Monthly costLow after infra setup
ASR + intent on Jetson. LLM on an on-premises GPU server (1× RTX 4090 or A10G). Optional internet for external APIs only (weather, VMS).
Latency Optimization:
Latency optimizationsimplement in phase 1–2
Whisper int8 quantization on Jetson
Cuts ASR time from 800ms → 380ms. Use ONNX Runtime or TensorRT.
Streaming TTS — speak before full response
Buffer 15 tokens, start audio. Perceived latency drops by ~1s.
Parallel intent + entity extraction
asyncio.gather() — run together, not sequentially.
Redis cache for repeat queries
FAQ answers, room lookups, schedule queries. Cache TTL = 5 minutes.
VAD pre-warming of context
Load session state from Redis the moment speech onset is detected.
GPT-4o mini for intent, GPT-4o for generation
Mini is 3× faster and sufficient for classification tasks.
Reliability + failure handlingcritical gaps to close
Graceful ASR failure handling
If Whisper confidence <0.6, robot says "Could you repeat that?" — not crash or silence.
LLM timeout fallback
Set 3s hard timeout. If exceeded, use scripted fallback: "Let me check and get back to you."
Network partition handling
If LAN server unreachable, Jetson falls back to on-device Llama 3.2-3B for basic responses.
Conversation repair logic
Detect topic switch mid-conversation. Reset state cleanly — don't carry stale entities.
Wake-word or proximity trigger
Don't run ASR 24/7. Trigger only when someone is <1.5m away (LiDAR) or says wake word.
Input sanitization before LLM
Strip prompt injection attempts. Enforce topic boundary — robot only talks about office-relevant topics.
Production infrastructureplan from week 1
Docker containers for all services
ASR service, dialogue service, RAG service, TTS service — each independently restartable.
Watchdog + auto-restart
systemd or supervisord. If any NLP service crashes, restart within 3 seconds — robot shouldn't freeze.
Health check endpoints
FastAPI /health on each service. LAN server pings every 10s. Robotics team can query NLP status.
Model versioning
MLflow or DVC. Never push a new model directly to the G1 — version, test, then deploy.
What this architecture can't handle yet (honest gaps)phase 3–4 or future
Simultaneous multi-person conversation
Speaker diarization (pyannote) needed. Not in v1 scope — one speaker at a time.
Noisy environment robustness
Open office with AC and chatter degrades Whisper WER significantly. Add noise suppression (RNNoise) as preprocessing.
Truly adaptive learning in production
Online fine-tuning on the G1 is not feasible. Improvement cycle is: log → annotate → retrain offline → deploy. Weekly cadence.
Full offline RAG without LAN server
Jetson Orin has 16GB RAM. Running Whisper + Llama 3B + Chroma simultaneously will be tight. Needs profiling.

The direct answers to your three questions
Will all these processes slow things down? Not if you architect around parallelism and streaming from day one. The architecture I showed earlier runs steps concurrently — the killer mistake is running them as a sequential chain. With the optimizations above, 1.2–1.5s end-to-end is realistic, and because TTS streams, the robot feels even faster because it starts speaking before processing is complete.
Is this production ready? The architecture is sound, but production readiness is a property you earn through the reliability work — timeouts, fallbacks, watchdogs, health checks — not just accuracy. A 95% accurate NLP system that hard-crashes on the 5% is not production ready. The red items in the checklist above are what you need to close before calling it production-grade. Realistically, that's weeks 15–18 of the roadmap.
Internet requirement? With the hybrid setup, internet becomes optional, not required. The critical path — someone speaks, robot understands, robot responds, robot acts — runs entirely on the Jetson + LAN server. Internet is only needed for optional integrations: Google Calendar sync, external weather, VMS cloud APIs. If your client wants full air-gapped operation (common in corporate security environments), you replace those with on-premises equivalents: a locally hosted Nextcloud calendar, a local weather station feed, and an on-premises VMS. The LLM on your LAN GPU server (Llama 3.1-8B quantized) will cover 90% of conversational quality at zero marginal cost per query.
The one infrastructure investment that makes this all work: a single on-premises server with an RTX 4090 or A10G GPU. That's your LAN inference node — it runs the LLM, RAG, and dialogue manager, serving the G1 over WiFi at LAN speeds (~1ms RTT). Everything else is software.
