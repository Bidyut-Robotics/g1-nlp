#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# entrypoint.sh — GPU/CPU detection and environment setup
#
# Runs before the main application. Checks for NVIDIA GPU availability and
# sets ASR_DEVICE / ASR_COMPUTE_TYPE accordingly.  If no GPU is found the
# pipeline falls back to CPU-optimised settings automatically.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

echo "=================================================="
echo " Humanoid NLP — Container Startup"
echo "=================================================="

# ── GPU detection ─────────────────────────────────────────────────────────────
GPU_AVAILABLE=false
if command -v nvidia-smi &>/dev/null && nvidia-smi --query-gpu=name --format=csv,noheader &>/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
    GPU_AVAILABLE=true
    echo "[HARDWARE] GPU detected: ${GPU_NAME}"
else
    echo "[HARDWARE] No NVIDIA GPU found — falling back to CPU mode"
fi

# ── GPU mode: use CUDA + GPU-accelerated ONNX runtime ─────────────────────────
if [ "$GPU_AVAILABLE" = "true" ]; then
    export ASR_DEVICE="${ASR_DEVICE:-cuda}"
    export ASR_COMPUTE_TYPE="${ASR_COMPUTE_TYPE:-float16}"
    export NVIDIA_VISIBLE_DEVICES="${NVIDIA_VISIBLE_DEVICES:-all}"

    # Swap onnxruntime for onnxruntime-gpu if not already installed
    if ! python3 -c "import onnxruntime; assert 'CUDAExecutionProvider' in onnxruntime.get_available_providers()" 2>/dev/null; then
        echo "[SETUP] Installing onnxruntime-gpu for CUDA acceleration..."
        pip install --quiet --no-cache-dir --force-reinstall "onnxruntime-gpu>=1.18.0"
    else
        echo "[SETUP] onnxruntime-gpu with CUDA already available."
    fi

    echo "[HARDWARE] Mode: GPU (device=${ASR_DEVICE}, compute=${ASR_COMPUTE_TYPE})"

# ── CPU fallback ───────────────────────────────────────────────────────────────
else
    export ASR_DEVICE="cpu"
    export ASR_COMPUTE_TYPE="int8"
    # Ensure CPU onnxruntime is present (not the GPU build)
    if ! python3 -c "import onnxruntime" 2>/dev/null; then
        echo "[SETUP] Installing onnxruntime (CPU)..."
        pip install --quiet --no-cache-dir "onnxruntime>=1.15.0"
    fi
    echo "[HARDWARE] Mode: CPU fallback (device=cpu, compute=int8)"
fi

# ── Hardware mode summary ──────────────────────────────────────────────────────
echo "[CONFIG] HARDWARE_MODE=${HARDWARE_MODE:-laptop}"
echo "[CONFIG] LLM_MODE=${LLM_MODE:-local}"
echo "[CONFIG] ASR_DEVICE=${ASR_DEVICE}"
echo "[CONFIG] ASR_COMPUTE_TYPE=${ASR_COMPUTE_TYPE}"
echo "=================================================="

exec "$@"
