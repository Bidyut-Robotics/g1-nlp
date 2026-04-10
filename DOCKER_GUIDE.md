# 🐳 Docker Guide: Humanoid NLP

This guide explains how to run the `humanoid_nlp` project using Docker with full NVIDIA GPU acceleration and audio support.

## 📋 Prerequisites

1. **NVIDIA Container Toolkit**:
   Ensure you have the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) installed on your host.
2. **PulseAudio**:
   The container uses the host's PulseAudio socket for audio.
3. **Docker 28.1.1+** and **Compose 2.35.1+**.

---

## 🚀 Quick Start (Laptop Mode)

1. **Setup Environment**:

   ```bash
   cp .env.example .env
   # Edit .env and add your GROQ_API_KEY if using cloud LLM
   ```

2. **Build and Start**:

   ```bash
   docker compose up --build -d
   ```

3. **Initialize Models**:
   Ollama runs as a sidecar. You need to pull the models you want to use:

   ```bash
   # Pull the default 1B model (fastest)
   bash scripts/pull_models.sh llama3.2:1b
   ```

4. **Verify Audio & GPU**:
   Check the logs to see if the GPU was detected and PulseAudio is connected:
   ```bash
   docker compose logs -f nlp
   ```
   _Look for: `[HARDWARE] GPU detected: ...` and `[HARDWARE] Mode: GPU ...`_

---

## 🤖 Running on G1 Robot

To use the robot's microphone and speaker while running inside Docker:

1. **Start the G1 Audio Bridge (on the host)**:
   The bridge connects the robot via DDS to your host's PulseAudio.

   ```bash
   python services/hardware/g1_hardware_manager.py start
   ```

2. **Start Docker in G1 Mode**:
   ```bash
   HARDWARE_MODE=g1 docker compose up -d
   ```
   _The container will now read from the `g1_microphone` PulseAudio source._

---

## 🛠️ Common Commands

| Command                        | Purpose                           |
| ------------------------------ | --------------------------------- |
| `docker compose up -d`         | Start services in background      |
| `docker compose stop`          | Stop services                     |
| `docker compose logs -f nlp`   | Follow application logs           |
| `docker compose exec nlp bash` | Open a shell inside the container |
| `docker compose restart nlp`   | Restart the NLP service           |

---

## 📝 Troubleshooting

### No Audio inside Container

Check if PulseAudio is running on the host:

```bash
pactl info
```

If the container logs say `Connection refused`, ensure the socket `/tmp/pulse/pulse-native` exists on your host.

### GPU Not Detected

Ensure `nvidia-smi` works on your host. If it works on host but not in container, try:

```bash
docker run --rm --runtime=nvidia --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
```

If this fails, NVIDIA Container Toolkit is not configured correctly.

### Port Conflicts

If port `11434` is already used by a local Ollama instance, stop it or change the port mapping in `docker-compose.yml`.
