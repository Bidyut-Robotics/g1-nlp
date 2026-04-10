# ──────────────────────────────────────────────────────────────────────────────
# Stage 1: Base — CUDA 12.4 + cuDNN + Ubuntu 22.04
# ──────────────────────────────────────────────────────────────────────────────
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 AS base

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

# GPU-specific: install onnxruntime-gpu as default inside the image.
# The entrypoint will fall back to the CPU build if no GPU is detected at runtime.
RUN pip install --no-cache-dir --force-reinstall "onnxruntime-gpu>=1.18.0"

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
