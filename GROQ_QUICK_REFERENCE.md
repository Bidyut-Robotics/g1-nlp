# ⚡ Groq vs Local Quick Reference

## 🚀 TL;DR - Start with Groq

```bash
# 1. Get API key: https://console.groq.com (free)
# 2. Set environment
export GROQ_API_KEY="your-key"

# 3. Install groq package
pip install groq

# 4. Run (config already defaults to groq)
python main.py

# 5. Test: "What time is it?" → instant
#    Test: "Tell me a joke" → 10-15 seconds
```

---

## 📊 Mode Comparison

```
┌─────────────────────────┬──────────────────┬──────────────────┐
│ Feature                 │ Groq (Cloud)     │ Ollama (Local)   │
├─────────────────────────┼──────────────────┼──────────────────┤
│ Response Speed          │ 10-15s ⚡⚡⚡    │ 45-60s ⚡        │
│ Setup Time              │ 2 min ✅         │ 20+ min ✅       │
│ Internet Required       │ Yes ✅           │ No ✅            │
│ Works Offline           │ No ✅            │ Yes ✅           │
│ Privacy                 │ Data → Groq      │ Fully private    │
│ Cost                    │ Free tier + paid │ Free             │
│ Quality                 │ Excellent ⭐⭐⭐│ Good ⭐⭐      │
│ For Testing             │ Perfect! 🎯      │ Backup option    │
│ For Production          │ Good option      │ Best (offline)   │
└─────────────────────────┴──────────────────┴──────────────────┘
```

---

## 🔄 Switch Modes

### Option 1: Config File (Persistent)
```bash
# Edit: config/app_config.json
# Change: "mode": "groq" to "mode": "local"
```

### Option 2: Environment Variable (Override)
```bash
export LLM_MODE=groq    # Use Groq
export LLM_MODE=local   # Use Ollama
python main.py
```

### Option 3: Both Running (Fallback)
```bash
# Terminal 1: Start Ollama
ollama serve

# Terminal 2: Run with Groq (falls back to Ollama if API fails)
export GROQ_API_KEY="your-key"
python main.py
```

---

## ⚙️ Configuration

### Groq Settings (cloud)
```json
"groq": {
  "model": "mixtral-8x7b-32768",  // Change model here
  "temperature": 0.1,              // 0=strict, 1=creative
  "max_tokens": 150                // Max response length
}
```

### Ollama Settings (local)
```json
"local": {
  "model_name": "phi3",            // Change model here
  "base_url": "http://localhost:11434",
  "temperature": 0.1,
  "num_predict": 50,               // Max response tokens
  "num_ctx": 512                   // Context window
}
```

---

## 🎯 Usage Scenarios

### Scenario 1: Quick Testing (Do This First)
```bash
# Use Groq for fastest feedback
export GROQ_API_KEY="your-key"
python main.py
# ✅ Instant responses, great for testing
```

### Scenario 2: Offline Development
```bash
# Switch to Ollama
# In config: "mode": "local"
ollama serve
python main.py
# ✅ Works offline, slower but functional
```

### Scenario 3: Production on Jetson
```bash
# Use Ollama with lightweight model
# In config: change to "llama3.2:1b"
# Expected: 12-18 second responses
# ✅ Fast, private, edge device
```

---

## 📋 Groq Model Options

| Model | Speed | Best For | Tokens/s |
|-------|-------|----------|----------|
| mixtral-8x7b | ⚡⚡⚡ | Voice (default) | 50+ |
| llama-3-8b | ⚡⚡⚡ | Simple Q&A | 100+ |
| llama-3-70b | ⚡⚡ | Complex reasoning | 20+ |
| gemma-7b | ⚡⚡ | Lightweight | 80+ |

```bash
# Change model
export GROQ_MODEL="llama-3-8b-8192"
python main.py
```

---

## 🔐 API Key Setup

### Get Free Groq Key
```bash
# 1. Visit: https://console.groq.com
# 2. Sign up (free, instant)
# 3. Click "API Keys"
# 4. Create new key → Copy
# 5. Store securely
```

### Set in Environment
```bash
# Temporary (for this session)
export GROQ_API_KEY="gsk_..."

# Permanent (add to ~/.bashrc or ~/.zshrc)
echo 'export GROQ_API_KEY="gsk_..."' >> ~/.bashrc
source ~/.bashrc
```

### Verify It's Set
```bash
echo $GROQ_API_KEY
# Should print: gsk_...
```

---

## ✅ Testing Checklist

- [ ] Groq API key obtained from console.groq.com
- [ ] Environment variable set: `GROQ_API_KEY`
- [ ] Package installed: `pip install groq`
- [ ] Config checked (defaults to groq mode)
- [ ] First test: `python main.py` with simple query
- [ ] Monitor response time: Should be 10-15 seconds
- [ ] Verify no "[AI Thinking - Second Pass]" messages
- [ ] Check logs for streaming to audio

---

## 🐛 Quick Fixes

| Problem | Solution |
|---------|----------|
| Still slow | Try: `export GROQ_MODEL="llama-3-8b-8192"` |
| "API key not set" | Check: `echo $GROQ_API_KEY` |
| "401 Unauthorized" | Create new key at groq.com/console |
| "Rate limited" | Wait 1-2 min or upgrade plan |
| Want offline mode | Change config to "mode": "local" |

---

## 📈 Expected Performance

### With Groq Cloud
```
User speaks → ASR (4s) → Groq LLM (10s) → TTS (2s) = 16s total
First audio feedback within 2-3s of speaking ✅
```

### With Ollama Local
```
User speaks → ASR (4s) → Ollama LLM (45s) → TTS (2s) = 51s total
First audio feedback within 20s of speaking ⚠️
```

### With Jetson + Ollama
```
User speaks → ASR (2s) → Ollama LLM (15s) → TTS (2s) = 19s total
First audio feedback within 8s of speaking ✅
```

---

## 🚀 You're Ready!

1. ✅ Code integrated (GroqLLM + Factory)
2. ✅ Config ready (defaults to groq)
3. ✅ Both modes supported (groq + local)
4. ✅ Easy to switch (one line change)
5. ✅ Documented (setup guides provided)

**Next step:** Install groq and test!

```bash
pip install groq
export GROQ_API_KEY="your-key"
python main.py
```

Enjoy conversational speed! 🎉
