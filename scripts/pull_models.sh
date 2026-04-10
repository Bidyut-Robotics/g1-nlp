#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# pull_models.sh — Pull required Ollama model(s) into the running sidecar
#
# Usage:
#   bash scripts/pull_models.sh                  # pulls default model
#   bash scripts/pull_models.sh llama3.2:1b      # pulls a specific model
#   bash scripts/pull_models.sh phi3              # laptop/CPU testing model
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

MODEL="${1:-llama3.2:1b}"
OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"

echo "[pull_models] Waiting for Ollama to be ready at ${OLLAMA_URL} ..."
for i in $(seq 1 30); do
  if curl -sf "${OLLAMA_URL}/api/tags" > /dev/null 2>&1; then
    echo "[pull_models] Ollama is up."
    break
  fi
  echo "[pull_models] Not ready yet (attempt ${i}/30) — retrying in 3s..."
  sleep 3
done

echo "[pull_models] Pulling model: ${MODEL}"
curl -sf -X POST "${OLLAMA_URL}/api/pull" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"${MODEL}\"}" | \
  while IFS= read -r line; do
    status=$(echo "$line" | jq -r '.status // empty' 2>/dev/null || true)
    [ -n "$status" ] && echo "  → $status"
  done

echo "[pull_models] Done. Model '${MODEL}' is ready."
