"""Live microphone test utility for openWakeWord."""

from __future__ import annotations

import argparse
import queue
import sys
import time
from typing import Iterable

import numpy as np


SAMPLE_RATE = 16000
BLOCK_SIZE = 1280  # 80 ms at 16 kHz, which matches openWakeWord's preferred chunk size.


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test openWakeWord from your system microphone."
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available input devices and exit.",
    )
    parser.add_argument(
        "--device",
        type=int,
        help="Input device index from --list-devices.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Print a detection when any score is at or above this value.",
    )
    parser.add_argument(
        "--model",
        dest="models",
        action="append",
        default=[],
        help=(
            "Optional wake-word model name or model path. Repeat this flag to load "
            "multiple models. If omitted, openWakeWord tries to load all packaged models."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print the top score every second even when below the detection threshold.",
    )
    parser.add_argument(
        "--download-models",
        action="store_true",
        help="Download the missing openWakeWord model files into the active Python environment and exit.",
    )
    return parser.parse_args()


def list_input_devices() -> None:
    import sounddevice as sd

    print("Available input devices:")
    found = False
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] <= 0:
            continue
        found = True
        print(
            f"[{index}] {device['name']} | "
            f"channels={device['max_input_channels']} | "
            f"default_sr={int(device['default_samplerate'])}"
        )

    if not found:
        print("No microphone input devices were reported by PortAudio.")


def create_model(models: Iterable[str]):
    from openwakeword.model import Model

    try:
        if models:
            return Model(wakeword_models=list(models))
        return Model()
    except Exception as exc:  # pragma: no cover - depends on local install state
        raise RuntimeError(
            "Failed to initialize openWakeWord. "
            "If you did not pass `--model`, your installed package may be missing its "
            "bundled model files. Reinstall `openwakeword` or pass one or more valid "
            "model names/paths with `--model`."
        ) from exc


def download_models(models: Iterable[str]) -> None:
    from openwakeword.utils import download_models as openwakeword_download_models

    selected_models = list(models)
    openwakeword_download_models(model_names=selected_models)

    if selected_models:
        print(f"Downloaded model assets for: {', '.join(selected_models)}")
    else:
        print("Downloaded model assets for all bundled openWakeWord models.")


def audio_callback(
    indata: np.ndarray,
    frames: int,
    time_info,
    status,
    audio_queue: "queue.Queue[np.ndarray]",
) -> None:
    if status:
        print(f"[audio] {status}", file=sys.stderr)

    chunk = np.squeeze(indata.copy())
    if chunk.ndim == 0:
        chunk = np.array([chunk], dtype=np.int16)

    if chunk.dtype != np.int16:
        chunk = chunk.astype(np.int16)

    audio_queue.put(chunk)


def main() -> int:
    args = parse_args()

    if args.list_devices:
        list_input_devices()
        return 0

    if args.download_models:
        download_models(args.models)
        return 0

    import sounddevice as sd

    model = create_model(args.models)
    audio_queue: "queue.Queue[np.ndarray]" = queue.Queue()
    last_debug_print = 0.0

    print("Wake-word mic test started.")
    print("Speak near the selected microphone. Press Ctrl+C to stop.")
    print(f"Sample rate: {SAMPLE_RATE} Hz | Block size: {BLOCK_SIZE} samples")
    if args.device is not None:
        print(f"Using input device index: {args.device}")

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        blocksize=BLOCK_SIZE,
        device=args.device,
        channels=1,
        dtype="int16",
        callback=lambda indata, frames, time_info, status: audio_callback(
            indata, frames, time_info, status, audio_queue
        ),
    ):
        while True:
            chunk = audio_queue.get()
            predictions = model.predict(chunk)
            best_label, best_score = max(predictions.items(), key=lambda item: item[1])

            if best_score >= args.threshold:
                print(f"[detected] {best_label}: {best_score:.3f}")
                continue

            if args.debug and time.time() - last_debug_print >= 1.0:
                print(f"[debug] top score -> {best_label}: {best_score:.3f}")
                last_debug_print = time.time()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped microphone test.")
