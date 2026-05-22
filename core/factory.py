import os
from .interfaces import IASRProvider, ILLMProvider, ITTSProvider
from .config import get_llm_config, get_tts_config, load_app_config
from services.asr.asr_service import FasterWhisperASR, ParakeetASR
from services.reasoning.llm_service import OllamaLLM, GroqLLM
from services.reasoning.openai_service import EnterpriseChatGPT
from services.tts.tts_service import PiperTTS, G1BuiltinTTS, G1DirectTTS, G1BridgeTTS


class ServiceFactory:
    """
    Factory to return Local or Remote providers based on configuration.
    Priority: env var > app_config.json > hardcoded default.

    LLM modes:
      groq       → GroqLLM (Groq cloud, fastest for dev)
      local      → OllamaLLM (on-device, Jetson-ready)
      enterprise → EnterpriseChatGPT (client's custom endpoint)
    """

    @staticmethod
    def get_asr_provider() -> IASRProvider:
        cfg = load_app_config().get("asr", {})
        mode = os.getenv("ASR_MODE", cfg.get("mode", "whisper")).lower()
        device = os.getenv("ASR_DEVICE", cfg.get("device", "cpu"))

        if mode == "parakeet":
            return ParakeetASR(device=device)

        # default: whisper
        model_size = os.getenv("ASR_MODEL_SIZE", cfg.get("model_size", "tiny"))
        compute_type = os.getenv("ASR_COMPUTE_TYPE", cfg.get("compute_type", "int8"))
        return FasterWhisperASR(
            model_size=model_size,
            device=device,
            compute_type=compute_type,
        )

    @staticmethod
    def get_llm_provider() -> ILLMProvider:
        llm_config = get_llm_config()
        mode = os.getenv("LLM_MODE", llm_config.get("mode", "groq")).lower()

        if mode == "groq":
            groq_config = llm_config.get("groq", {})
            return GroqLLM(
                model_name=os.getenv("GROQ_MODEL", groq_config.get("model", "llama-3.1-8b-instant")),
                api_key=os.getenv("GROQ_API_KEY"),
                temperature=float(os.getenv("LLM_TEMPERATURE", str(groq_config.get("temperature", 0.1)))),
                max_tokens=int(os.getenv("LLM_MAX_TOKENS", str(groq_config.get("max_tokens", 150)))),
            )

        elif mode == "local":
            local_config = llm_config.get("local", {})
            return OllamaLLM(
                model_name=os.getenv("LLM_MODEL", local_config.get("model_name", "llama3.2:1b")),
                base_url=os.getenv("OLLAMA_BASE_URL", local_config.get("base_url", "http://localhost:11434")),
                temperature=float(os.getenv("LLM_TEMPERATURE", str(local_config.get("temperature", 0.1)))),
                num_predict=int(os.getenv("LLM_NUM_PREDICT", str(local_config.get("num_predict", 50)))),
                num_ctx=int(os.getenv("LLM_NUM_CTX", str(local_config.get("num_ctx", 512)))),
                keep_alive=os.getenv("LLM_KEEP_ALIVE", local_config.get("keep_alive", "30m")),
            )

        elif mode == "enterprise":
            ent_config = llm_config.get("enterprise", {})
            return EnterpriseChatGPT(
                api_key=os.getenv("ENTERPRISE_API_KEY"),
                base_url=os.getenv("ENTERPRISE_API_BASE", ent_config.get("base_url", "https://api.openai.com/v1")),
                model_name=os.getenv("ENTERPRISE_MODEL", ent_config.get("model", "gpt-4o")),
                temperature=float(os.getenv("LLM_TEMPERATURE", str(ent_config.get("temperature", 0.1)))),
                max_tokens=int(os.getenv("LLM_MAX_TOKENS", str(ent_config.get("max_tokens", 200)))),
            )

        else:
            print(f"[FACTORY] Unknown LLM mode '{mode}', falling back to groq.")
            return GroqLLM(
                model_name=os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
                api_key=os.getenv("GROQ_API_KEY"),
            )

    @staticmethod
    def get_tts_provider() -> ITTSProvider:
        tts_config = get_tts_config()
        mode = os.getenv("TTS_MODE", tts_config.get("mode", "local")).lower()

        if mode == "local":
            model_path = os.getenv(
                "TTS_MODEL_PATH",
                tts_config.get("model_path", "models/en_US-lessac-medium.onnx"),
            )
            return PiperTTS(model_path=model_path)
        elif mode == "g1_direct":
            # Priorities: Env Var > app_config.json > eth0
            g1_cfg = load_app_config().get("g1", {})
            interface = os.getenv(
                "G1_DDS_INTERFACE", 
                g1_cfg.get("dds_interface", "eth0")
            )
            model_path = os.getenv(
                "TTS_MODEL_PATH",
                tts_config.get("model_path", "models/en_US-lessac-medium.onnx"),
            )
            return G1DirectTTS(model_path=model_path, interface=interface)
        elif mode == "g1_builtin":
            # Native Robot Voice
            g1_cfg = load_app_config().get("g1", {})
            interface = os.getenv("G1_DDS_INTERFACE", g1_cfg.get("dds_interface", "eth0"))
            speaker_id = int(os.getenv("G1_SPEAKER_ID", "1"))
            return G1BuiltinTTS(interface=interface, speaker_id=speaker_id)
        elif mode == "g1_bridge":
            # Piper on AGX → robot_agent TCP → DDS PlayStream
            model_path = os.getenv(
                "TTS_MODEL_PATH",
                tts_config.get("model_path", "models/en_US-lessac-medium.onnx"),
            )
            agent_host = os.getenv("ROBOT_AGENT_HOST", "127.0.0.1")
            agent_port = int(os.getenv("ROBOT_AGENT_PORT", "7789"))
            return G1BridgeTTS(model_path=model_path, agent_host=agent_host, agent_port=agent_port)
        else:
            return PiperTTS()
