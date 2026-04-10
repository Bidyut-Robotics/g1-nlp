#!/usr/bin/env python3
"""
G1 hardware startup helper.
Run this script with `python services/hardware/g1_hardware_manager.py start`
to launch the G1 audio driver as a background subprocess before starting main.py.

Usage
-----
  # Start driver in background
  python services/hardware/g1_hardware_manager.py start

  # Check status
  python services/hardware/g1_hardware_manager.py status

  # Stop driver
  python services/hardware/g1_hardware_manager.py stop
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DRIVER_SCRIPT = Path(__file__).parent / "g1_audio_driver.py"
PID_FILE = Path("/tmp/g1_audio_driver.pid")


def _write_pid(pid: int):
    PID_FILE.write_text(str(pid))


def _read_pid() -> int | None:
    if PID_FILE.exists():
        try:
            return int(PID_FILE.read_text().strip())
        except ValueError:
            pass
    return None


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def cmd_start(args):
    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"[G1 DRIVER] Already running (PID {pid}).")
        return

    driver_args = [sys.executable, str(DRIVER_SCRIPT)]
    if args.no_mic:
        driver_args.append("--no-mic")
    if args.no_speaker:
        driver_args.append("--no-speaker")
    if args.verbose:
        driver_args.append("--verbose")

    proc = subprocess.Popen(
        driver_args,
        stdout=open("/tmp/g1_audio_driver.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _write_pid(proc.pid)
    time.sleep(2)
    if _is_running(proc.pid):
        print(f"[G1 DRIVER] Started (PID {proc.pid}). Log: /tmp/g1_audio_driver.log")
    else:
        print("[G1 DRIVER] Failed to start. Check /tmp/g1_audio_driver.log")
        sys.exit(1)


def cmd_stop(args):
    pid = _read_pid()
    if not pid:
        print("[G1 DRIVER] No PID file found. Is the driver running?")
        return
    if not _is_running(pid):
        print(f"[G1 DRIVER] Process {pid} not running. Cleaning up PID file.")
        PID_FILE.unlink(missing_ok=True)
        return
    os.kill(pid, signal.SIGTERM)
    time.sleep(1)
    if not _is_running(pid):
        PID_FILE.unlink(missing_ok=True)
        print(f"[G1 DRIVER] Stopped (PID {pid}).")
    else:
        print(f"[G1 DRIVER] Process {pid} did not stop cleanly.")


def cmd_status(args):
    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"[G1 DRIVER] Running (PID {pid}).")
    else:
        print("[G1 DRIVER] Not running.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="G1 audio driver manager")
    sub = parser.add_subparsers()

    p_start = sub.add_parser("start", help="Start the G1 audio driver")
    p_start.add_argument("--no-mic", action="store_true")
    p_start.add_argument("--no-speaker", action="store_true")
    p_start.add_argument("--verbose", "-v", action="store_true")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="Stop the G1 audio driver")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="Check driver status")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()
