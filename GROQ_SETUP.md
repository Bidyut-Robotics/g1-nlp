# 🚀 Groq Cloud LLM Setup Guide

## Quick Start: Switch to Groq (Cloud Testing)

### Step 1: Get Groq API Key
```bash
# Visit: https://console.groq.com
# Sign up → Create API Key → Copy it
# Free tier available, very generous!
```

### Step 2: Set Environment Variable
```bash
export GROQ_API_KEY="your-api-key-here"
```

Or add to your `.bashrc` / `.zshrc`:
```bash
echo 'export GROQ_API_KEY="your-api-key-here"' >> ~/.bashrc
source ~/.bashrc
```

### Step 3: Install Groq Package
```bash
pip install groq
```

### Step 4: Update Config (if needed)
The config is already set to use Groq by default. Check `config/app_config.json`:

```json
{
  "llm": {
    "mode": "groq",      ← Change this to "local" for Ollama
    "groq": {
      "model": "mixtral-8x7b-32768",
      "temperature": 0.1,
      "max_tokens": 150
    },
    "local": {
      "model_name": "phi3",
      "base_url": "http://localhost:11434",
      "temperature": 0.1,
      "num_predict": 50,
      "num_ctx": 512,
      "keep_alive": "30m"
    }
  }
}
```

### Step 5: Run Assistant
```bash
cd /home/surya/my_research/humanoid_nlp
source .venv/bin/activate
python main.py
```

Expected first response: **10-15 seconds** (streaming starts within 5s)

---

## 📋 Configuration Options

### Use Groq Cloud
```json
"mode": "groq"
```
- ✅ Ultra-fast (10-100 tokens/s)
- ✅ No local setup needed
- ✅ Free tier available
- ❌ Requires internet

### Use Local Ollama
```json
"mode": "local"
```
- ✅ Completely offline
- ✅ No API costs
- ❌ Slower on CPU (1-3 tokens/s)
- ❌ Requires ollama running

### Switch Modes

**Option 1: Edit config file**
```bash
# Edit config/app_config.json
# Change: "mode": "groq" to "mode": "local"
```

**Option 2: Environment variable (overrides config)**
```bash
export LLM_MODE=groq    # Use Groq
export LLM_MODE=local   # Use Ollama
python main.py
```

---

## 🔧 Groq Model Options

Available fast models:

| Model | Speed | Best For |
|-------|-------|----------|
| `mixtral-8x7b-32768` | ⚡⚡⚡ Fast | Voice assistant (default) |
| `llama-3-70b-8192` | ⚡⚡⚡ Fast | Complex reasoning |
| `llama-3-8b-8192` | ⚡⚡⚡ Fastest | Simple Q&A |
| `gemma-7b-it` | ⚡⚡ Very fast | Lightweight responses |

Change in `config/app_config.json`:
```json
"groq": {
  "model": "llama-3-8b-8192"
}
```

Or via environment:
```bash
export GROQ_MODEL="llama-3-8b-8192"
python main.py
```

---

## 📊 Performance Comparison

| Setup | First Token | Full Response | Notes |
|-------|------------|---------------|-------|
| Groq (cloud) | 2-3s | 10-15s | ✅ Fastest, needs internet |
| Ollama (CPU) | 15-20s | 45-60s | ✅ Offline, slower |
| Ollama (Jetson) | 5-8s | 15-25s | ✅ Best of both |

---

## 🐛 Troubleshooting

### Error: "GROQ_API_KEY not set"
```bash
# Check if key is set:
echo $GROQ_API_KEY

# If empty, set it:
export GROQ_API_KEY="gsk_..."
```

### Error: "401 Unauthorized"
- API key is invalid or expired
- Go to https://console.groq.com and create new key
- Copy exact key (no spaces, no quotes)

### Error: "Rate limited"
- Free tier has limits
- Wait a few minutes or upgrade plan
- https://console.groq.com/account/billing/overview

### Still slow?
1. Check first token time (should be 2-3s)
2. Check internet connection
3. Try different model: `llama-3-8b-8192`
4. Switch to local: `"mode": "local"`

---

## 💡 Why Groq?

For testing voice assistant, Groq is ideal because:

| Feature | Groq | Local Ollama |
|---------|------|-------------|
| Setup time | 2 minutes | 20+ minutes |
| Response speed | 10-15s | 45-60s |
| Quality | Excellent | Good |
| Cost | Free tier | Free |
| Internet required | Yes | No |
| Privacy | Data to Groq | Private |

---

## 🎯 Recommended Setup for Testing

```bash
# Terminal 1: Start assistant with Groq
export GROQ_API_KEY="your-key"
python main.py

# Terminal 2 (optional): Monitor performance
# Watch for logs like:
# [AI Thinking]: Response appearing within 5-10s
# [ROBOT] <response>
```

---

## 🔄 Switching Back to Local

When ready to test locally:

```bash
# Edit config/app_config.json:
# Change "mode": "groq" to "mode": "local"

# Then start ollama in one terminal:
ollama serve

# And run assistant in another:
python main.py
```

---

## 📚 More Info

- **Groq Docs:** https://console.groq.com/docs
- **API Reference:** https://console.groq.com/keys
- **Pricing:** Free tier is very generous!
- **Status:** https://status.groq.com

---

## ✅ You're All Set!

1. ✅ API key from Groq console
2. ✅ Environment variable set: `GROQ_API_KEY`
3. ✅ Package installed: `pip install groq`
4. ✅ Config ready (already set to groq mode)
5. ✅ Ready to test: `python main.py`

**Expected result:** Fast, conversational responses (10-15 seconds) 🚀
