# ──────────────────────────────────────────────────────────────────────────────
# Stage 1: Base — CUDA 11.8 + cuDNN 8 + Ubuntu 22.04
# Match G1's 11.4 driver while allowing modern dependencies
# ──────────────────────────────────────────────────────────────────────────────
FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04 AS base

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# System packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Python 3.11
    python3.11 \
    python3.11-dev \
    python3.11-venv \
    python3-pip \
    # Build tools
    build-essential \
    cmake \
    git \
    curl \
    wget \
    ca-certificates \
    # Audio system (ALSA + PulseAudio client)
    alsa-utils \
    pulseaudio \
    pulseaudio-utils \
    libasound2 \
    libasound2-dev \
    libportaudio2 \
    libportaudiocpp0 \
    portaudio19-dev \
    # Piper TTS shared libs
    libespeak-ng1 \
    libopus0 \
    libsndfile1 \
    # Faster-Whisper / ONNX native deps
    libgomp1 \
    # Network utilities (for DDS / multicast)
    iproute2 \
    iputils-ping \
    net-tools \
    # Misc
    ffmpeg \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Make python3.11 the default python
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python  python  /usr/bin/python3.11 1

# Upgrade pip
RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# ──────────────────────────────────────────────────────────────────────────────
# Stage 2: Dependencies — install all Python packages
# ──────────────────────────────────────────────────────────────────────────────
FROM base AS deps

WORKDIR /install

# Copy only requirements first (better layer caching)
COPY requirements.txt .

# Core dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Download missing wake-word models (not included in the standard pip package)
RUN mkdir -p /usr/local/lib/python3.11/dist-packages/openwakeword/resources/models/ && \
    wget -q https://github.com/dscripka/openWakeWord/raw/main/openwakeword/resources/models/hey_jarvis_v0.1.onnx \
    -O /usr/local/lib/python3.11/dist-packages/openwakeword/resources/models/hey_jarvis_v0.1.onnx

# NOTE: onnxruntime-gpu is notoriously difficult to install via pip on ARM64/Jetson.
# We default to the 'onnxruntime' (CPU) package from requirements.txt for the image build.
# Our entrypoint.sh will automatically use 'cpu/int8' modes if GPU acceleration isn't 
# perfectly configured, ensuring the robot ALWAYS responds.
#
# If you are on x86_64 with a GPU and want maximum speed, you can manually run:
# pip install onnxruntime-gpu

# ──────────────────────────────────────────────────────────────────────────────
# Stage 3: App — final lean image
# ──────────────────────────────────────────────────────────────────────────────
FROM deps AS app

WORKDIR /app

# PulseAudio client config: allow connections from the host socket
RUN mkdir -p /root/.config/pulse && \
    echo "default-server = unix:/tmp/pulse/pulse-native" > /root/.config/pulse/client.conf && \
    echo "autospawn = no" >> /root/.config/pulse/client.conf && \
    echo "daemon-binary = /bin/true" >> /root/.config/pulse/client.conf

# Copy application source
COPY . .

# Copy and register the entrypoint (GPU/CPU detection + env setup)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Environment defaults overridden by entrypoint.sh at runtime
ENV NLP_DEBUG=1
ENV HARDWARE_MODE=laptop
ENV LLM_MODE=local
ENV OLLAMA_BASE_URL=http://ollama:11434

# ASR defaults — entrypoint.sh will override these based on GPU availability:
#   GPU present  → ASR_DEVICE=cuda, ASR_COMPUTE_TYPE=float16
#   No GPU       → ASR_DEVICE=cpu,  ASR_COMPUTE_TYPE=int8
ENV ASR_DEVICE=cpu
ENV ASR_COMPUTE_TYPE=int8

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "main.py"]
